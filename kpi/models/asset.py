#!/usr/bin/python
# -*- coding: utf-8 -*-
# 😬

import re
import sys
import copy
import json
import StringIO
from collections import OrderedDict

import xlwt
import six
from django.conf import settings as django_settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.exceptions import MultipleObjectsReturned
from django.db import models
from django.db import transaction
from django.db.models import Prefetch
from django.dispatch import receiver
from django.utils.translation import ugettext_lazy as _
import jsonbfield.fields
from jsonfield import JSONField
from jsonbfield.fields import JSONField as JSONBField
from taggit.managers import TaggableManager, _TaggableManager
from taggit.utils import require_instance_manager
from bs4 import BeautifulSoup

from pyxform import builder, xls2json
from pyxform.errors import PyXFormError

from formpack import FormPack
from formpack.utils.flatten_content import flatten_content
from formpack.utils.json_hash import json_hash
from formpack.utils.spreadsheet_content import flatten_to_spreadsheet_content
from asset_version import AssetVersion
from kpi.utils.standardize_content import (standardize_content,
                                           needs_standardization,
                                           standardize_content_in_place)
from kpi.utils.autoname import (autoname_fields_in_place,
                                autovalue_choices_in_place)
from kpi.constants import ASSET_TYPES, ASSET_TYPE_BLOCK,\
    ASSET_TYPE_QUESTION, ASSET_TYPE_SURVEY, ASSET_TYPE_TEMPLATE
from .object_permission import ObjectPermission, ObjectPermissionMixin
from ..fields import KpiUidField, LazyDefaultJSONBField
from ..utils.asset_content_analyzer import AssetContentAnalyzer
from ..utils.sluggify import sluggify_label
from ..utils.kobo_to_xlsform import (to_xlsform_structure,
                                     expand_rank_and_score_in_place,
                                     replace_with_autofields,
                                     remove_empty_expressions_in_place)
from ..utils.asset_translation_utils import (
        compare_translations,
        # TRANSLATIONS_EQUAL,
        TRANSLATIONS_OUT_OF_ORDER,
        TRANSLATION_RENAMED,
        TRANSLATION_DELETED,
        TRANSLATION_ADDED,
        TRANSLATION_CHANGE_UNSUPPORTED,
        TRANSLATIONS_MULTIPLE_CHANGES,
    )
from ..utils.random_id import random_id
from ..deployment_backends.mixin import DeployableMixin
from kobo.apps.reports.constants import (SPECIFIC_REPORTS_KEY,
                                         DEFAULT_REPORTS_KEY)
from kpi.utils.log import logging

CUSTOM_COL_APPEND_STRING = 'custom_col_append_string'

# TODO: Would prefer this to be a mixin that didn't derive from `Manager`.
class TaggableModelManager(models.Manager):

    def create(self, *args, **kwargs):
        tag_string= kwargs.pop('tag_string', None)
        created= super(TaggableModelManager, self).create(*args, **kwargs)
        if tag_string:
            created.tag_string= tag_string
        return created


class KpiTaggableManager(_TaggableManager):
    @require_instance_manager
    def add(self, *tags, **kwargs):
        ''' A wrapper that replaces spaces in tag names with dashes and also
        strips leading and trailng whitespace. Behavior should match the
        TagsInput transform function in app.es6. '''
        tags_out = []
        for t in tags:
            # Modify strings only; the superclass' add() method will then
            # create Tags or use existing ones as appropriate.  We do not fix
            # existing Tag objects, which could also be passed into this
            # method, because a fixed name could collide with the name of
            # another Tag object already in the database.
            if isinstance(t, six.string_types):
                t = t.strip().replace(' ', '-')
            tags_out.append(t)
        super(KpiTaggableManager, self).add(*tags_out, **kwargs)


class AssetManager(TaggableModelManager):
    def filter_by_tag_name(self, tag_name):
        return self.filter(tags__name=tag_name)


# TODO: Merge this functionality into the eventual common base class of `Asset`
# and `Collection`.
class TagStringMixin:

    @property
    def tag_string(self):
        try:
            tag_list = self.prefetched_tags
        except AttributeError:
            tag_names = self.tags.values_list('name', flat=True)
        else:
            tag_names = [t.name for t in tag_list]
        return ','.join(tag_names)

    @tag_string.setter
    def tag_string(self, value):
        intended_tags = value.split(',')
        self.tags.set(*intended_tags)

FLATTEN_OPTS = {
    'remove_columns': {
        'survey': [
            '$prev',
            'select_from_list_name',
            '_or_other',
        ],
        'choices': []
    },
    'remove_sheets': [
        'schema',
    ],
}


class OCFormUtils(object):

    def _adjust_content_custom_column(self, content):
        survey = content.get('survey', [])
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            if 'readonly' in survey_col:
                readonly_val = survey_col['readonly'].lower()
                if readonly_val == 'yes' or readonly_val == 'true':
                    readonly_val = 'true'
                else:
                    readonly_val = 'false'
                content['survey'][survey_col_idx]['oc_readonly'] = readonly_val
                del content['survey'][survey_col_idx]['readonly']
            else:
                content['survey'][survey_col_idx]['oc_readonly'] = 'false'

    def _adjust_content_media_column(self, content):
        survey = content.get('survey', [])
        non_dc_media_columns = ['audio', 'image', 'video']
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            for non_dc_media_column in non_dc_media_columns:
                oc_non_dc_media_column = "oc_{}".format(non_dc_media_column)
                if oc_non_dc_media_column in survey_col.keys():
                    survey_col[non_dc_media_column] = survey_col[oc_non_dc_media_column]
                    del survey_col[oc_non_dc_media_column]

        translated = content.get('translated', [])
        for translated_idx in range(len(translated)):
            for non_dc_media_column in non_dc_media_columns:
                oc_non_dc_media_column = "oc_{}".format(non_dc_media_column)
                if oc_non_dc_media_column == translated[translated_idx]:
                    translated[translated_idx] = non_dc_media_column
    
    def _adjust_content_media_column_before_standardize(self, content):
        
        def _adjust_media_columns(survey, non_dc_cols):
            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                survey_col_keys = list(survey_col.keys())
                for survey_col_key in survey_col_keys:
                    if survey_col_key in non_dc_cols:
                        survey_col["oc_{}".format(survey_col_key)] = survey_col[survey_col_key]
                        del survey_col[survey_col_key]
        
        survey = content.get('survey', [])

        survey_col_key_list = []
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            survey_col_key_list = survey_col_key_list + list(survey_col.keys())

        media_columns = {"audio": "media::audio", "image": "media::image", "video": 'media::video'}

        for media_column_key in media_columns.keys():
            non_dc_col = media_column_key
            non_dc_cols = [s for s in survey_col_key_list if s.startswith(non_dc_col)]

            if len(non_dc_cols) > 0:
                _adjust_media_columns(survey, non_dc_cols)

        if 'translations' in content:
            translated = content.get('translated', [])
            non_dc_media_columns = ['audio', 'image', 'video']
            for translated_idx in range(len(translated)):
                for non_dc_media_column in non_dc_media_columns:
                    if non_dc_media_column == translated[translated_idx]:
                        translated[translated_idx] = "oc_{}".format(non_dc_media_column)

    def _revert_custom_column(self, content):
        survey = content.get('survey', [])
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            if 'oc_readonly' in survey_col:
                content['survey'][survey_col_idx]['readonly'] = survey_col['oc_readonly']
                del content['survey'][survey_col_idx]['oc_readonly']


class FormpackXLSFormUtils(object):
    
    def _standardize(self, content):
        if needs_standardization(content):
            standardize_content_in_place(content)
            return True
        else:
            return False
    
    def _autoname(self, content):
        autoname_fields_in_place(content, '$autoname')
        autovalue_choices_in_place(content, '$autovalue')

    def _populate_fields_with_autofields(self, content):
        replace_with_autofields(content)

    def _expand_kobo_qs(self, content):
        expand_rank_and_score_in_place(content)

    def _ensure_settings(self, content):
        # asset.settings should exist already, but
        # on some legacy forms it might not
        _settings = content.get('settings', {})
        if isinstance(_settings, list):
            if len(_settings) > 0:
                _settings = _settings[0]
            else:
                _settings = {}
        if not isinstance(_settings, dict):
            _settings = {}
        content['settings'] = _settings

    def _append(self, content, **sheet_data):
        settings = sheet_data.pop('settings', None)
        if settings:
            self._ensure_settings(content)
            content['settings'].update(settings)
        for (sht, rows) in sheet_data.items():
            if sht in content:
                content[sht] += rows

    def _xlsform_structure(self, content, ordered=True, kobo_specific=False):
        opts = copy.deepcopy(FLATTEN_OPTS)

        # Remove hxl column and value from XLS export
        opts['remove_columns']['survey'].append('hxl')

        if not kobo_specific:
            opts['remove_columns']['survey'].append('$kuid')
            opts['remove_columns']['survey'].append('$autoname')
            opts['remove_columns']['choices'].append('$kuid')
            opts['remove_columns']['choices'].append('$autovalue')
        if ordered:
            if not isinstance(content, OrderedDict):
                raise TypeError('content must be an ordered dict if '
                                'ordered=True')
            flatten_to_spreadsheet_content(content, in_place=True,
                                           **opts)
        else:
            flatten_content(content, in_place=True, **opts)

    def _assign_kuids(self, content):
        for row in content['survey']:
            if '$kuid' not in row:
                row['$kuid'] = random_id(9)
        for row in content.get('choices', []):
            if '$kuid' not in row:
                row['$kuid'] = random_id(9)

    def _strip_kuids(self, content):
        # this is important when stripping out kobo-specific types because the
        # $kuid field in the xform prevents cascading selects from rendering
        for row in content['survey']:
            row.pop('$kuid', None)
        for row in content.get('choices', []):
            row.pop('$kuid', None)

    def _link_list_items(self, content):
        arr = content['survey']
        if len(arr) > 0:
            arr[0]['$prev'] = None
        for i in range(1, len(arr)):
            arr[i]['$prev'] = arr[i-1]['$kuid']

    def _unlink_list_items(self, content):
        arr = content['survey']
        for row in arr:
            if '$prev' in row:
                del row['$prev']

    def _remove_empty_expressions(self, content):
        remove_empty_expressions_in_place(content)

    def _make_default_translation_first(self, content):
        # The form builder only shows the first language, so make sure the
        # default language is always at the top of the translations list. The
        # new translations UI, on the other hand, shows all languages:
        # https://github.com/kobotoolbox/kpi/issues/1273
        try:
            default_translation_name = content['settings']['default_language']
        except KeyError:
            # No `default_language`; don't do anything
            return
        else:
            self._prioritize_translation(content, default_translation_name)

    def _strip_empty_rows(self, content, vals=None):
        if vals is None:
            vals = {
                u'survey': u'type',
                u'choices': u'list_name',
            }
        for (sheet_name, required_key) in vals.iteritems():
            arr = content.get(sheet_name, [])
            arr[:] = [row for row in arr if required_key in row]

    def pop_setting(self, content, *args):
        if 'settings' in content:
            return content['settings'].pop(*args)

    def _rename_null_translation(self, content, new_name):
        if new_name in content['translations']:
            raise ValueError('Cannot save translation with duplicate '
                             'name: {}'.format(new_name))

        try:
            _null_index = content['translations'].index(None)
        except ValueError:
            raise ValueError('Cannot save translation name: {}'.format(
                             new_name))
        content['translations'][_null_index] = new_name

    def _has_translations(self, content, min_count=1):
        return len(content.get('translations', [])) >= min_count

    def update_translation_list(self, translation_list):
        existing_ts = self.content.get('translations', [])
        params = compare_translations(existing_ts,
                                      translation_list)
        if None in translation_list and translation_list[0] is not None:
            raise ValueError('Unnamed translation must be first in '
                             'list of translations')
        if TRANSLATIONS_OUT_OF_ORDER in params:
            self._reorder_translations(self.content, translation_list)
        elif TRANSLATION_RENAMED in params:
            _change = params[TRANSLATION_RENAMED]['changes'][0]
            self._rename_translation(self.content, _change['from'],
                                     _change['to'])
        elif TRANSLATION_ADDED in params:
            if None in existing_ts:
                raise ValueError('cannot add translation if an unnamed translation exists')
            self._prepend_translation(self.content, params[TRANSLATION_ADDED])
        elif TRANSLATION_DELETED in params:
            if params[TRANSLATION_DELETED] != existing_ts[-1]:
                raise ValueError('you can only delete the last translation of the asset')
            self._remove_last_translation(self.content)
        else:
            for chg in [
                        TRANSLATIONS_MULTIPLE_CHANGES,
                        TRANSLATION_CHANGE_UNSUPPORTED,
                        ]:
                if chg in params:
                    raise ValueError(
                        'Unsupported change: "{}": {}'.format(
                            chg,
                            params[chg]
                            )
                    )

    def _prioritize_translation(self, content, translation_name, is_new=False):
        # the translations/languages present this particular content
        _translations = content['translations']
        # the columns that have translations
        _translated = content.get('translated', [])
        if is_new and (translation_name in _translations):
            raise ValueError('cannot add existing translation')
        elif (not is_new) and (translation_name not in _translations):
            # if there are no translations available, don't try to prioritize,
            # just ignore the translation `translation_name`
            if len(_translations) == 1 and _translations[0] is None:
                return
            else:  # Otherwise raise an error.
                # Remove None from translations we want to display to users
                valid_translations = [t for t in _translations if t is not None]
                raise ValueError("`{translation_name}` is specified as the default language, "
                                 "but only these translations are present in the form: `{translations}`".format(
                                    translation_name=translation_name,
                                    translations="`, `".join(valid_translations)
                                    )
                                 )

        _tindex = -1 if is_new else _translations.index(translation_name)
        if is_new or (_tindex > 0):
            for sheet_name in 'survey', 'choices':
                for row in content.get(sheet_name, []):
                    for col in _translated:
                        if is_new:
                            val = '{}'.format(row[col][0])
                        else:
                            try:
                                val = row[col].pop(_tindex)
                            except KeyError:
                                continue
                            except AttributeError:
                                continue
                        row[col].insert(0, val)
            if is_new:
                _translations.insert(0, translation_name)
            else:
                _translations.insert(0, _translations.pop(_tindex))

    def _remove_last_translation(self, content):
        content.get('translations').pop()
        _translated = content.get('translated', [])
        for row in content.get('survey', []):
            for col in _translated:
                row[col].pop()
        for row in content.get('choices', []):
            for col in _translated:
                row[col].pop()

    def _prepend_translation(self, content, translation_name):
        self._prioritize_translation(content, translation_name, is_new=True)

    def _reorder_translations(self, content, translations):
        _ts = translations[:]
        _ts.reverse()
        for _tname in _ts:
            self._prioritize_translation(content, _tname)

    def _rename_translation(self, content, _from, _to):
        _ts = content.get('translations')
        if _to in _ts:
            raise ValueError('Duplicate translation: {}'.format(_to))
        _ts[_ts.index(_from)] = _to


class XlsExportable(object):
    
    surveyCols = [
        u'type',
        u'name',
        u'label',
        u'bind::oc:itemgroup',
        u'hint',
        u'appearance',
        u'bind::oc:briefdescription',
        u'bind::oc:description',
        u'relevant',
        u'required',
        u'required_message',
        u'constraint',
        u'constraint_message',
        u'default',
        u'calculation',
        u'trigger',
        u'readonly',
        u'image',
        u'repeat_count',
        u'bind::oc:external',
        u'bind::oc:contactdata',
        u'instance::oc:contactdata'
    ]

    choicesCols = [
        u'list_name',
        u'label',
        u'name', 
        u'image'
    ]
    
    def ordered_xlsform_content(self,
                                kobo_specific_types=False,
                                append=None):
        # currently, this method depends on "FormpackXLSFormUtils"
        content = copy.deepcopy(self.content)
        if append:
            self._append(content, **append)
        self._survey_prepare_custom_col_value(content)
        self._adjust_content_media_column_before_standardize(content)
        self._standardize(content)
        self._adjust_content_media_column(content)
        self._survey_revert_custom_col_value(content)
        if not kobo_specific_types:
            self._expand_kobo_qs(content)
            self._autoname(content)
            self._populate_fields_with_autofields(content)
            self._strip_kuids(content)
        content = OrderedDict(content)
        self._survey_column_oc_adjustments(content)
        self._settings_ensure_form_id(content)
        self._settings_ensure_required_columns(content)
        self._settings_maintain_key_order(content)
        self._choices_column_oc_adjustments(content)
        self._xlsform_structure(content, ordered=True, kobo_specific=kobo_specific_types)
        self._survey_maintain_key_order(content)
        self._choices_maintain_key_order(content)
        return content

    def _survey_prepare_custom_col_value(self, content):
        survey = content.get('survey', [])
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            if 'required' in survey_col:
                if survey_col['required'] == True:
                    survey_col['required'] = 'yes+{}'.format(CUSTOM_COL_APPEND_STRING)
                elif survey_col['required'] == False:
                    survey_col['required'] = '+{}'.format(CUSTOM_COL_APPEND_STRING)
            if 'readonly' in survey_col:
                if survey_col['readonly'] == 'true':
                    survey_col['oc_readonly'] = 'yes+{}'.format(CUSTOM_COL_APPEND_STRING)
                elif survey_col['readonly'] == 'false':
                    survey_col['oc_readonly'] = '+{}'.format(CUSTOM_COL_APPEND_STRING)
                del survey_col['readonly']

    def _survey_revert_custom_col_value(self, content):
        survey = content.get('survey', [])
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            if 'required' in survey_col:
                if CUSTOM_COL_APPEND_STRING in survey_col['required']:
                    req_col_append_string_pos = survey_col['required'].find(CUSTOM_COL_APPEND_STRING)
                    survey_col['required'] = survey_col['required'][:req_col_append_string_pos - 1]
            if 'oc_readonly' in survey_col:
                if CUSTOM_COL_APPEND_STRING in survey_col['oc_readonly']:
                    req_col_append_string_pos = survey_col['oc_readonly'].find(CUSTOM_COL_APPEND_STRING)
                    survey_col['readonly'] = survey_col['oc_readonly'][:req_col_append_string_pos - 1]
                del survey_col['oc_readonly']

    def _survey_column_oc_adjustments(self, content):
        survey = content.get('survey', [])

        if len(survey) > 0:
            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                
                for surveyCol in self.surveyCols:
                    if surveyCol not in survey_col:
                        if 'translated' in content.keys() and surveyCol in content['translated']:
                            survey_col[surveyCol] = [u''] * len(content['translations'])
                        else:
                            survey_col[surveyCol] = u''

                if '$given_name' in survey_col:
                    del survey_col['$given_name']

                if 'type' in survey_col:
                    if survey_col['type'] == 'begin_group':
                        survey_col['type'] = 'begin group'
                    elif survey_col['type'] == 'end_group':
                        survey_col['type'] = 'end group'
                    elif survey_col['type'] == 'begin_repeat':
                        survey_col['type'] = 'begin repeat'
                    elif survey_col['type'] == 'end_repeat':
                        survey_col['type'] = 'end repeat'
                    elif survey_col['type'] == 'select_one_from_file':
                        select_one_filename = 'codelist.csv'
                        if 'select_one_from_file_filename' in survey_col and survey_col['select_one_from_file_filename'].strip() != '':
                            select_one_filename = survey_col['select_one_from_file_filename']
                        survey_col['type'] = 'select_one_from_file' + ' ' + select_one_filename

                if 'select_one_from_file_filename' in survey_col:
                    del survey_col['select_one_from_file_filename']
        else:
            cols = OrderedDict()
            for surveyCol in self.surveyCols:
                if 'translated' in content.keys() and surveyCol in content['translated']:
                    cols[surveyCol] = [u''] * len(content['translations'])
                else:
                    cols[surveyCol] = u''
            content['survey'].append(cols)

    def _survey_maintain_key_order(self, content):
        if 'survey' in content:
            survey = content['survey']
            
            # Maintains key order of survey sheet
            surveyKeyOrder = self.surveyCols
            
            for idx, col in enumerate(survey):
                surveyRemainingKeyOrder = []
                for surveyKey in col.keys():
                    if surveyKey not in surveyKeyOrder:
                        surveyRemainingKeyOrder.append(surveyKey)
                surveyKeyOrder = surveyKeyOrder + surveyRemainingKeyOrder

                content['survey'][idx] = OrderedDict(sorted(col.items(), key=lambda i:surveyKeyOrder.index(i[0])))
    
    def _settings_ensure_form_id(self, content):
        # Show form_id and remove id_string in downloaded xls
        if 'settings' in content:
            settings = content['settings']
            
            # Remove id_string from settings sheet
            if 'id_string' in settings:
                settings['form_id'] = settings['id_string']
                del settings['id_string']
    
    def _settings_ensure_required_columns(self, content):
        if 'settings' in content:
            settings = content['settings']

            try:
                form_title = self.name
            except Exception as e:
                form_title = "Form Title"

            settings.update(
                {
                    'form_title': form_title,
                    'crossform_references': '',
                    'namespaces': 'oc="http://openclinica.org/xforms" , OpenClinica="http://openclinica.com/odm"',
                    'Read Me - Form template created by OpenClinica Form Designer': ''
                }
            )

    def _settings_maintain_key_order(self, content):
        if 'settings' in content:
            settings = content['settings']
            
            # Maintains key order of settings sheet
            settingsKeyOrder = [
                'form_title',
                'form_id',
                'version', 
                'style',
                'crossform_references',
                'namespaces', 
                'Read Me - Form template created by OpenClinica Form Designer'
            ]
            settingsRemainingKeyOrder = []
            for settingsKey in settings.keys():
                if settingsKey not in settingsKeyOrder:
                    settingsRemainingKeyOrder.append(settingsKey)

            settingsKeyOrder = settingsKeyOrder + settingsRemainingKeyOrder
            content['settings'] = OrderedDict(sorted(settings.items(), key=lambda i:settingsKeyOrder.index(i[0])))
    
    def _choices_column_oc_adjustments(self, content):
        choices = content.get('choices', [])

        if len(choices) > 0:
            for choices_col_idx in range(len(choices)):
                choices_col = choices[choices_col_idx]

                for choicesCol in self.choicesCols:
                    if choicesCol not in choices_col:
                        if 'translated' in content.keys() and choicesCol in content['translated']:
                            choices_col[choicesCol] = [u''] * len(content['translations'])
                        else:
                            choices_col[choicesCol] = u''
        else:
            cols = OrderedDict()
            for choicesCol in self.choicesCols:
                if 'translated' in content.keys() and choicesCol in content['translated']:
                    cols[choicesCol] = [u''] * len(content['translations'])
                else:
                    cols[choicesCol] = u''
            content[u'choices'] = []
            content[u'choices'].append(cols)
    
    def _choices_maintain_key_order(self, content):
        if 'choices' in content:
            choices = content['choices']
            
            # Maintains key order of choices sheet
            choiceKeyOrder = self.choicesCols
            
            for idx, choice in enumerate(choices):
                choiceRemainingKeyOrder = []
                for choiceKey in choice.keys():
                    if choiceKey not in choiceKeyOrder:
                        choiceRemainingKeyOrder.append(choiceKey)
                choiceKeyOrder = choiceKeyOrder + choiceRemainingKeyOrder

                content['choices'][idx] = OrderedDict(sorted(choice.items(), key=lambda i:choiceKeyOrder.index(i[0])))

    def to_xls_io(self, versioned=False, **kwargs):
        ''' To append rows to one or more sheets, pass `append` as a
        dictionary of lists of dictionaries in the following format:
            `{'sheet name': [{'column name': 'cell value'}]}`
        Extra settings may be included as a dictionary in the same
        parameter.
            `{'settings': {'setting name': 'setting value'}}` '''
        if versioned:
            append = kwargs['append'] = kwargs.get('append', {})
            append_settings = append['settings'] = append.get('settings', {})
        try:
            def _add_contents_to_sheet(sheet, contents):
                cols = []
                for row in contents:
                    for key in row.keys():
                        if key not in cols:
                            cols.append(key)
                for ci, col in enumerate(cols):
                    sheet.write(0, ci, col)
                for ri, row in enumerate(contents):
                    for ci, col in enumerate(cols):
                        val = row.get(col, None)
                        if val:
                            sheet.write(ri + 1, ci, val)
            # The extra rows and settings should persist within this function
            # and its return value *only*. Calling deepcopy() is required to
            # achieve this isolation.
            ss_dict = self.ordered_xlsform_content(**kwargs)
            ordered_ss_dict = OrderedDict()
            for t in ['settings', 'choices', 'survey']:
                if t in ss_dict:
                    ordered_ss_dict[t] = ss_dict[t]

            workbook = xlwt.Workbook()
            
            for (sheet_name, contents) in ordered_ss_dict.iteritems():
                cur_sheet = workbook.add_sheet(sheet_name)
                _add_contents_to_sheet(cur_sheet, contents)
        except Exception as e:
            six.reraise(
                Exception,
                "asset.content improperly formatted for XLS "
                "export: %s" % repr(e),
                sys.exc_info()[2]
            )

        string_io = StringIO.StringIO()
        workbook.save(string_io)
        string_io.seek(0)
        return string_io


class Asset(ObjectPermissionMixin,
            TagStringMixin,
            DeployableMixin,
            XlsExportable,
            FormpackXLSFormUtils,
            OCFormUtils,
            models.Model):
    name = models.CharField(max_length=255, blank=True, default='')
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)
    content = JSONField(null=True)
    summary = JSONField(null=True, default=dict)
    report_styles = JSONBField(default=dict)
    report_custom = JSONBField(default=dict)
    map_styles = LazyDefaultJSONBField(default=dict)
    map_custom = LazyDefaultJSONBField(default=dict)
    asset_type = models.CharField(
        choices=ASSET_TYPES, max_length=20, default=ASSET_TYPE_SURVEY)
    parent = models.ForeignKey(
        'Collection', related_name='assets', null=True, blank=True)
    owner = models.ForeignKey('auth.User', related_name='assets', null=True)
    editors_can_change_permissions = models.BooleanField(default=True)
    uid = KpiUidField(uid_prefix='a')
    tags = TaggableManager(manager=KpiTaggableManager)
    settings = jsonbfield.fields.JSONField(default=dict)

    # _deployment_data should be accessed through the `deployment` property
    # provided by `DeployableMixin`
    _deployment_data = JSONField(default=dict)

    permissions = GenericRelation(ObjectPermission)

    objects = AssetManager()

    @property
    def kind(self):
        return 'asset'

    class Meta:
        ordering = ('-date_modified',)

        permissions = (
            # change_, add_, and delete_asset are provided automatically
            # by Django
            ('view_asset', _('Can view asset')),
            ('share_asset', _("Can change asset's sharing settings")),
            # Permissions for collected data, i.e. submissions
            ('add_submissions', _('Can submit data to asset')),
            ('view_submissions', _('Can view submitted data for asset')),
            ('change_submissions', _('Can modify submitted data for asset')),
            ('delete_submissions', _('Can delete submitted data for asset')),
            ('share_submissions', _("Can change sharing settings for "
                                    "asset's submitted data")),
            ('validate_submissions', _("Can validate submitted data asset")),
            # TEMPORARY Issue #1161: A flag to indicate that permissions came
            # solely from `sync_kobocat_xforms` and not from any user
            # interaction with KPI
            ('from_kc_only', 'INTERNAL USE ONLY; DO NOT ASSIGN')
        )

    # Assignable permissions that are stored in the database
    ASSIGNABLE_PERMISSIONS = (
        'view_asset',
        'change_asset',
        'add_submissions',
        'view_submissions',
        'change_submissions',
        'validate_submissions',
    )
    # Calculated permissions that are neither directly assignable nor stored
    # in the database, but instead implied by assignable permissions
    CALCULATED_PERMISSIONS = (
        'share_asset',
        'delete_asset',
        'share_submissions',
        'delete_submissions'
    )
    # Certain Collection permissions carry over to Asset
    MAPPED_PARENT_PERMISSIONS = {
        'view_collection': 'view_asset',
        'change_collection': 'change_asset'
    }
    # Granting some permissions implies also granting other permissions
    IMPLIED_PERMISSIONS = {
        # Format: explicit: (implied, implied, ...)
        'change_asset': ('view_asset',),
        'add_submissions': ('view_asset',),
        'view_submissions': ('view_asset',),
        'change_submissions': ('view_submissions',),
        'validate_submissions': ('view_submissions',)
    }
    # Some permissions must be copied to KC
    KC_PERMISSIONS_MAP = { # keys are KC's codenames, values are KPI's
        'change_submissions': 'change_xform', # "Can Edit" in KC UI
        'view_submissions': 'view_xform', # "Can View" in KC UI
        'add_submissions': 'report_xform', # "Can submit to" in KC UI
        'validate_submissions': 'validate_xform',  # "Can Validate" in KC UI
    }
    KC_CONTENT_TYPE_KWARGS = {'app_label': 'logger', 'model': 'xform'}
    # KC records anonymous access as flags on the `XForm`
    KC_ANONYMOUS_PERMISSIONS_XFORM_FLAGS = {
        'view_submissions': {'shared': True, 'shared_data': True}
    }

    # todo: test and implement this method
    # def restore_version(self, uid):
    #     _version_to_restore = self.asset_versions.get(uid=uid)
    #     self.content = _version_to_restore.version_content
    #     self.name = _version_to_restore.name

    def to_ss_structure(self):
        return flatten_content(self.content, in_place=False)

    def _populate_summary(self):
        if self.content is None:
            self.content = {}
            self.summary = {}
            return
        analyzer = AssetContentAnalyzer(**self.content)
        self.summary = analyzer.summary

    def adjust_content_on_save(self):
        '''
        This is called on save by default if content exists.
        Can be disabled / skipped by calling with parameter:
        asset.save(adjust_content=False)
        '''
        self._adjust_content_custom_column(self.content)
        self._adjust_content_media_column_before_standardize(self.content)
        self._standardize(self.content)
        self._adjust_content_media_column(self.content)
        self._revert_custom_column(self.content)
        self._make_default_translation_first(self.content)
        self._strip_empty_rows(self.content)
        self._assign_kuids(self.content)
        self._autoname(self.content)
        self._unlink_list_items(self.content)
        self._remove_empty_expressions(self.content)

        settings = self.content['settings']
        _title = settings.pop('form_title', None)
        id_string = settings.get('id_string')
        filename = self.summary.pop('filename', None)
        if filename:
            # if we have filename available, set the id_string
            # and/or form_title from the filename.
            if not id_string:
                id_string = sluggify_label(filename)
                settings['id_string'] = id_string
            if not _title:
                _title = filename
        if not self.asset_type in [ASSET_TYPE_SURVEY, ASSET_TYPE_TEMPLATE]:
            # instead of deleting the settings, simply clear them out
            self.content['settings'] = {}

        if _title is not None:
            self.name = _title

    def save(self, *args, **kwargs):
        if self.content is None:
            self.content = {}

        # in certain circumstances, we don't want content to
        # be altered on save. (e.g. on asset.deploy())
        if kwargs.pop('adjust_content', True):
            self.adjust_content_on_save()

        # populate summary
        self._populate_summary()

        # infer asset_type only between question and block
        if self.asset_type in [ASSET_TYPE_QUESTION, ASSET_TYPE_BLOCK]:
            row_count = self.summary.get('row_count')
            if row_count == 1:
                self.asset_type = ASSET_TYPE_QUESTION
            elif row_count > 1:
                self.asset_type = ASSET_TYPE_BLOCK

        self._populate_report_styles()
        self._survey_column_oc_save_adjustments()

        _create_version = kwargs.pop('create_version', True)
        super(Asset, self).save(*args, **kwargs)

        if _create_version:
            self.asset_versions.create(name=self.name,
                                       version_content=self.content,
                                       _deployment_data=self._deployment_data,
                                       # asset_version.deployed is set in the
                                       # DeploymentSerializer
                                       deployed=False,
                                       )

    def _survey_column_oc_save_adjustments(self):
        survey = self.content.get('survey', [])
        if len(survey) > 0:
            select_one_file_col_found = False
            select_one_file_col = 'select_one_from_file'
            select_one_filename_col = 'select_one_from_file_filename'
            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                if 'type' in survey_col:
                    if survey_col['type'].find(select_one_file_col) is not -1:
                        select_one_file_col_found = True

            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                if select_one_file_col_found and select_one_filename_col not in survey_col:
                    survey_col[select_one_filename_col] = ''

            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                if 'type' in survey_col:
                    type_col = survey_col['type']
                    if type_col.find(select_one_file_col) is not -1 and len(type_col) != len(select_one_file_col):
                        survey_col[select_one_filename_col] = type_col[type_col.find(select_one_file_col) + len(select_one_file_col):].strip()
                        survey_col['type'] = select_one_file_col

    def rename_translation(self, _from, _to):
        if not self._has_translations(self.content, 2):
            raise ValueError('no translations available')
        self._rename_translation(self.content, _from, _to)

    def to_clone_dict(self, version_uid=None, version=None):
        """
        Returns a dictionary of the asset based on version_uid or version.
        If `version` is specified, there are no needs to provide `version_uid` and make another request to DB.
        :param version_uid: string
        :param version: AssetVersion
        :return: dict
        """

        if not isinstance(version, AssetVersion):
            if version_uid:
                version = self.asset_versions.get(uid=version_uid)
            else:
                version = self.asset_versions.first()

        return {
            'name': version.name,
            'content': version.version_content,
            'asset_type': self.asset_type,
            'tag_string': self.tag_string,
        }

    def clone(self, version_uid=None):
        # not currently used, but this is how "to_clone_dict" should work
        return Asset.objects.create(**self.to_clone_dict(version_uid))

    def revert_to_version(self, version_uid):
        av = self.asset_versions.get(uid=version_uid)
        self.content = av.version_content
        self.save()

    def _populate_report_styles(self):
        default = self.report_styles.get(DEFAULT_REPORTS_KEY, {})
        specifieds = self.report_styles.get(SPECIFIC_REPORTS_KEY, {})
        kuids_to_variable_names = self.report_styles.get('kuid_names', {})
        for (index, row) in enumerate(self.content.get('survey', [])):
            if '$kuid' not in row:
                if 'name' in row:
                    row['$kuid'] = json_hash([self.uid, row['name']])
                else:
                    row['$kuid'] = json_hash([self.uid, index, row])
            _identifier = row.get('name', row['$kuid'])
            kuids_to_variable_names[_identifier] = row['$kuid']
            if _identifier not in specifieds:
                specifieds[_identifier] = {}
        self.report_styles = {
            DEFAULT_REPORTS_KEY: default,
            SPECIFIC_REPORTS_KEY: specifieds,
            'kuid_names': kuids_to_variable_names,
        }

    def get_ancestors_or_none(self):
        # ancestors are ordered from farthest to nearest
        if self.parent is not None:
            return self.parent.get_ancestors(include_self=True)
        else:
            return None

    @property
    def latest_version(self):
        versions = None
        try:
            versions = self.prefetched_latest_versions
        except AttributeError:
            versions = self.asset_versions.order_by('-date_modified')
        try:
            return versions[0]
        except IndexError:
            return None

    @property
    def deployed_versions(self):
        return self.asset_versions.filter(deployed=True).order_by(
                                          '-date_modified')

    @property
    def latest_deployed_version(self):
        return self.deployed_versions.first()

    @property
    def version_id(self):
        # Avoid reading the propery `self.latest_version` more than once, since
        # it may execute a database query each time it's read
        latest_version = self.latest_version
        if latest_version:
            return latest_version.uid

    @property
    def version__content_hash(self):
        # Avoid reading the propery `self.latest_version` more than once, since
        # it may execute a database query each time it's read
        latest_version = self.latest_version
        if latest_version:
            return latest_version.content_hash

    @property
    def snapshot(self):
        return self._snapshot(regenerate=False)

    @transaction.atomic
    def _snapshot(self, regenerate=True):
        asset_version = self.latest_version

        try:
            snapshot = AssetSnapshot.objects.get(asset=self,
                                                 asset_version=asset_version)
            if regenerate:
                snapshot.delete()
                snapshot = False
        except AssetSnapshot.MultipleObjectsReturned:
            # how did multiple snapshots get here?
            snaps = AssetSnapshot.objects.filter(asset=self,
                                                 asset_version=asset_version)
            snaps.delete()
            snapshot = False
        except AssetSnapshot.DoesNotExist:
            snapshot = False

        if not snapshot:
            if self.name != '':
                form_title = self.name
            else:
                _settings = self.content.get('settings', {})
                form_title = _settings.get('id_string', 'Untitled')

            self._append(self.content, settings={
                'form_title': form_title,
            })
            snapshot = AssetSnapshot.objects.create(asset=self,
                                                    asset_version=asset_version,
                                                    source=self.content)
        return snapshot

    def __unicode__(self):
        return u'{} ({})'.format(self.name, self.uid)

    @property
    def has_active_hooks(self):
        """
        Returns if asset has active hooks.
        Useful to update `kc.XForm.has_kpi_hooks` field.
        :return: {boolean}
        """
        return self.hooks.filter(active=True).exists()

    @staticmethod
    def optimize_queryset_for_list(queryset):
        ''' Used by serializers to improve performance when listing assets '''
        queryset = queryset.defer(
            # Avoid pulling these `JSONField`s from the database because:
            #   * they are stored as plain text, and just deserializing them
            #     to Python objects is CPU-intensive;
            #   * they are often huge;
            #   * we don't need them for list views.
            'content', 'report_styles'
        ).select_related(
            'owner__username',
        ).prefetch_related(
            # We previously prefetched `permissions__content_object`, but that
            # actually pulled the entirety of each permission's linked asset
            # from the database! For now, the solution is to remove
            # `content_object` here *and* from
            # `ObjectPermissionNestedSerializer`.
            'permissions__permission',
            'permissions__user',
            # `Prefetch(..., to_attr='prefetched_list')` stores the prefetched
            # related objects in a list (`prefetched_list`) that we can use in
            # other methods to avoid additional queries; see:
            # https://docs.djangoproject.com/en/1.8/ref/models/querysets/#prefetch-objects
            Prefetch('tags', to_attr='prefetched_tags'),
            Prefetch(
                'asset_versions',
                queryset=AssetVersion.objects.order_by(
                    '-date_modified'
                ).only('uid', 'asset', 'date_modified', 'deployed'),
                to_attr='prefetched_latest_versions',
            ),
        )
        return queryset


class AssetSnapshot(models.Model, XlsExportable, FormpackXLSFormUtils, OCFormUtils):
    '''
    This model serves as a cache of the XML that was exported by the installed
    version of pyxform.

    TODO: come up with a policy to clear this cache out.
    DO NOT: depend on these snapshots existing for more than a day until a policy is set.
    '''
    xml = models.TextField()
    source = JSONField(null=True)
    details = JSONField(default=dict)
    owner = models.ForeignKey('auth.User', related_name='asset_snapshots', null=True)
    asset = models.ForeignKey(Asset, null=True)
    _reversion_version_id = models.IntegerField(null=True)
    asset_version = models.OneToOneField('AssetVersion',
                                             on_delete=models.CASCADE,
                                             null=True)
    date_created = models.DateTimeField(auto_now_add=True)
    uid = KpiUidField(uid_prefix='s')

    @property
    def content(self):
        return self.source

    def save(self, *args, **kwargs):
        if self.asset is not None:
            if self.source is None:
                if self.asset_version is None:
                    self.asset_version = self.asset.latest_version
                self.source = self.asset_version.version_content
            if self.owner is None:
                self.owner = self.asset.owner
        _note = self.details.pop('note', None)
        _source = copy.deepcopy(self.source)
        if _source is None:
            _source = {}

        self._adjust_content_media_column_before_standardize(_source)
        self._standardize(_source)
        self._adjust_content_media_column(_source)
        self._revert_custom_column(_source)
        self._make_default_translation_first(_source)
        self._strip_empty_rows(_source)
        self._autoname(_source)
        self._remove_empty_expressions(_source)
        _settings = _source.get('settings', {})
        form_title = _settings.get('form_title')
        id_string = _settings.get('id_string')

        (self.xml, self.details) = \
            self.generate_xml_from_source(_source,
                                          include_note=_note,
                                          root_node_name='data',
                                          form_title=form_title,
                                          id_string=id_string)
        self.source = _source
        return super(AssetSnapshot, self).save(*args, **kwargs)

    def _adjust_content_media_column_before_generate_xml(self, content):

        media_columns = {"audio": "media::audio", "image": "media::image", "video": 'media::video'}

        def _adjust_media_columns(survey, non_dc_cols):
            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]
                survey_col_keys = list(survey_col.keys())
                for survey_col_key in survey_col_keys:
                    if survey_col_key in non_dc_cols:
                        survey_col["{}".format(media_columns[survey_col_key])] = survey_col[survey_col_key]
                        del survey_col[survey_col_key]

        survey = content.get('survey', [])

        survey_col_key_list = []
        for survey_col_idx in range(len(survey)):
            survey_col = survey[survey_col_idx]
            survey_col_key_list = survey_col_key_list + list(survey_col.keys())

        for media_column_key in media_columns.keys():
            non_dc_col = media_column_key
            non_dc_cols = [s for s in survey_col_key_list if s.startswith(non_dc_col)]

            if len(non_dc_cols) > 0:
                _adjust_media_columns(survey, non_dc_cols)

        if 'translations' in content:
            translated = content.get('translated', [])
            non_dc_media_columns = ['audio', 'image', 'video']
            for translated_idx in range(len(translated)):
                for non_dc_media_column in non_dc_media_columns:
                    if non_dc_media_column == translated[translated_idx]:
                        translated[translated_idx] = u"{}".format(media_columns[non_dc_media_column])

    def _prepare_for_xml_pyxform_generation(self, content, id_string):
        if 'settings' in content:
            settings = content['settings']

            if 'id_string' not in settings.keys():
                settings['id_string'] = id_string

            settings['name'] = 'data'
            content['settings'] = [settings]

        if 'settings_header' not in content:
            content['settings_header'] = [
                {
                  "form_title": "",
                  "form_id": "",
                  "version": "",
                  "style": "",
                  "crossform_references": "",
                  "namespaces": "",
                  "Read Me - Form template created by OpenClinica Form Designer": ""
                }
            ]

        translations = None
        if 'translations' in content:
            translations = content['translations']
            if all(x is None for x in translations):
                del content['translations']

        if 'choices' in content:
            choices = content['choices']

            for choice_col_idx in range(len(choices)):
                choice_col = choices[choice_col_idx]

                if 'label' in choice_col:
                    choice_col['label'] = choice_col['label'][0]

        if 'choices_header' not in content:
            content['choices_header'] = [
                {
                  "list_name": "",
                  "label": "",
                  "name": "",
                  "image": ""
                }
            ]


        if 'survey' in content:
            survey = content['survey']
            translated = content.get('translated', [])

            for survey_col_idx in range(len(survey)):
                survey_col = survey[survey_col_idx]

                if 'label' in survey_col and len(translated) > 0 and 'label' not in translated and type(survey_col['label']) is list:
                    survey_col['label'] = survey_col['label'][0]

                if 'hint' in survey_col and len(translated) > 0 and 'hint' not in translated and type(survey_col['hint']) is list:
                    survey_col['hint'] = survey_col['hint'][0]

                if 'type' in survey_col:
                    if 'select_one' == survey_col['type'] and 'select_from_list_name' in survey_col.keys():
                        survey_col['type'] = "{0} {1}".format(survey_col['type'], survey_col['select_from_list_name'])
                        del survey_col['select_from_list_name']
                    elif 'select_one_from_file' == survey_col['type']:
                        select_one_from_file_filename = 'codelist.csv'
                        if 'select_one_from_file_filename' in survey_col.keys():
                            select_one_from_file_filename = survey_col['select_one_from_file_filename']
                            if not select_one_from_file_filename.endswith(('.csv', '.xml')):
                                select_one_from_file_filename = '{}.csv'.format(select_one_from_file_filename)
                            del survey_col['select_one_from_file_filename']
                        survey_col['type'] = "{0} {1}".format(survey_col['type'], select_one_from_file_filename)

                if len(translated) > 0:
                    for translated_column in translated:
                        if translated_column in survey_col:
                            translated_value = survey_col[translated_column]
                            del survey_col[translated_column]
                            for translation in translations:
                                column_value = translated_value[translations.index(translation)]
                                if translations.index(translation) == 0:
                                    survey_col['{}'.format(translated_column, translation)] = u"{}".format(column_value)
                                else:
                                    survey_col['{}::{}'.format(translated_column, translation)] = u"{}".format(column_value)

        if 'survey_header' not in content:
            content['survey_header'] = [{ col : "" for col in self.surveyCols}]


    def generate_xml_from_source(self,
                                 source,
                                 include_note=False,
                                 root_node_name='snapshot_xml',
                                 form_title=None,
                                 id_string=None):
        if form_title is None:
            form_title = 'Snapshot XML'
        if id_string is None:
            id_string = 'snapshot_xml'

        if include_note and 'survey' in source:
            _translations = source.get('translations', [])
            _label = include_note
            if len(_translations) > 0:
                _label = [_label for t in _translations]
            source['survey'].append({u'type': u'note',
                                     u'name': u'prepended_note',
                                     u'label': _label})

        source_copy = copy.deepcopy(source)
        self._expand_kobo_qs(source_copy)
        self._populate_fields_with_autofields(source_copy)
        self._strip_kuids(source_copy)
        self._settings_ensure_required_columns(source_copy)
        self._adjust_content_media_column_before_generate_xml(source_copy)

        warnings = []
        details = {}
        try:
            xml = FormPack({'content': source_copy},
                                root_node_name=root_node_name,
                                id_string=id_string,
                                title=form_title)[0].to_xml(warnings=warnings)

            details.update({
                u'status': u'success',
                u'warnings': warnings,
            })

        except PyXFormError as err:
            self._prepare_for_xml_pyxform_generation(source_copy, id_string=id_string)

            survey_json = xls2json.workbook_to_json(source_copy)
            survey = builder.create_survey_element_from_dict(survey_json)
            xml = survey.to_xml()

            details.update({
                u'status': u'success',
                u'warnings': warnings,
            })
        
        except Exception as err:
            err_message = unicode(err)
            logging.error('Failed to generate xform for asset', extra={
                'src': source,
                'id_string': id_string,
                'uid': self.uid,
                '_msg': err_message,
                'warnings': warnings,
            })
            xml = ''
            details.update({
                u'status': u'failure',
                u'error_type': type(err).__name__,
                u'error': err_message,
                u'warnings': warnings,
            })
        
        if xml != '':

            def bind_is_calculate_and_has_external_clinicaldata(tag):
                return tag.name == 'bind' \
                    and tag.has_attr('calculate') \
                    and tag.has_attr('oc:external') \
                    and tag['oc:external'] == 'clinicaldata'

            soup = BeautifulSoup(xml, 'xml')
            soup_find_clinicaldata= soup.find_all(bind_is_calculate_and_has_external_clinicaldata)
            clinicaldata_count = len(soup_find_clinicaldata)
            soup_find_all_instance = soup.find_all('instance')
            instance_count = len(soup_find_all_instance)

            oc_clinicaldata_soup = BeautifulSoup('<instance id="clinicaldata" src="{}"/>'.format(django_settings.ENKETO_FORM_OC_INSTANCE_URL), 'xml')
            if clinicaldata_count > 0:
                if instance_count == 0:
                    if soup.find('model') is not None:
                        soup.model.insert(1, oc_clinicaldata_soup.instance)
                else:
                    soup_find_instance = soup.find_all('instance')
                    instance_count = len(soup_find_instance)
                    soup_find_instance[instance_count - 1].insert_after(oc_clinicaldata_soup.instance)
            
            soup_body = soup.find('h:body')
            if 'class' in soup_body.attrs:
                if 'no-text-transform' not in soup_body['class']:
                    soup_body['class'] = soup_body['class'] + ' no-text-transform'
            else:
                soup_body['class'] = 'no-text-transform'

            xml = str(soup)

        return (xml, details)



@receiver(models.signals.post_delete, sender=Asset)
def post_delete_asset(sender, instance, **kwargs):
    # Remove all permissions associated with this object
    ObjectPermission.objects.filter_for_object(instance).delete()
    # No recalculation is necessary since children will also be deleted
