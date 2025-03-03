_ = require 'underscore'
Backbone = require 'backbone'
$ = require 'jquery'
$configs = require './model.configs'
$rowSelector = require './view.rowSelector'
$row = require './model.row'
$modelUtils = require './model.utils'
$viewTemplates = require './view.templates'
$viewUtils = require './view.utils'
$viewChoices = require './view.choices'
$viewParams = require './view.params'
$viewMandatorySetting = require './view.mandatorySetting'
$acceptedFilesView = require './view.acceptedFiles'
$viewRowDetail = require './view.rowDetail'
renderKobomatrix = require('js/formbuild/renderInBackbone').renderKobomatrix
_t = require('utils').t
arrayMiddleOut = require('utils').processArrayMiddleOut
alertify = require 'alertifyjs'

module.exports = do ->
  class BaseRowView extends Backbone.View
    tagName: "li"
    className: "survey__row  xlf-row-view xlf-row-view--depr"
    events:
      "drop": "drop"

    initialize: (opts)->
      @options = opts
      typeDetail = @model.get("type")
      @$el.attr("data-row-id", @model.cid)
      @ngScope = opts.ngScope
      @surveyView = @options.surveyView
      @model.on "detail-change", (key, value, ctxt)=>
        customEventName = $viewUtils.normalizeEventName("row-detail-change-#{key}")
        @$(".on-#{customEventName}").trigger(customEventName, key, value, ctxt)
      @repeatGroups = []
      @nonRepeatGroups = []
      @nonGroups = []
      @repeatGroupsItemGroupNames = []
      @repeatGroupsIntVals = []
      @nonRepeatGroupsItemGroupNames = []
      @nonRepeatGroupsIntVals = []
      @nonGroupsItemGroupNames = []
      @nonGroupsIntVals = []
      @itemGroupKey = 'bind::oc:itemgroup'

    drop: (evt, index)->
      @$el.trigger("update-sort", [@model, index])

    getApp: ->
      @surveyView.getApp()

    isGroup: (model) ->
      model.constructor.kls is "Group"

    isInGroup: (model) ->
      model._parent?._parent?.constructor.kls is "Group"

    isInRepeatGroup: (model) ->
      model._parent?._parent?._isRepeat() is true

    getFirstRepeatGroupUntilRoot: (model) ->
      if not model.hasOwnProperty('_parent')
        return null
      else
        if @isInGroup(model) and @isInRepeatGroup(model)
          return model._parent._parent
        else
          return @getFirstRepeatGroupUntilRoot(model._parent._parent)

    isInRepeatGroupUntilRoot: (model) ->
      @getFirstRepeatGroupUntilRoot(model)?

    processAllModels: (models) ->
      for model in models
        if @isGroup model
          if model.get('_isRepeat').get('value')?
            @repeatGroups.push model
          else
            @nonRepeatGroups.push model
          @processAllModels model.rows?.models
        else
          if not @isInGroup(model) and (model.cid != @model.cid) and (model.attributes[@itemGroupKey].get('value') isnt '')
            @nonGroups.push model

    processFieldModels: (models) ->
      if models.length > 0
        for model in models
          groupNames = @nonGroupsItemGroupNames
          groupIntVals = @nonGroupsIntVals
          if @isInGroup(model)
            groupNames = @nonRepeatGroupsItemGroupNames
            groupIntVals = @nonRepeatGroupsIntVals
            if @isInRepeatGroupUntilRoot model
              groupNames = @repeatGroupsItemGroupNames
              groupIntVals = @repeatGroupsIntVals
          itemGroupName = model.attributes[@itemGroupKey].get('value')
          if itemGroupName && itemGroupName != ''
            groupNames.push(itemGroupName)
            itemGroupIntVal = parseInt(itemGroupName.replace(/\D/g, ''), 10)
            groupIntVals.push(itemGroupIntVal) if not isNaN(itemGroupIntVal)
        _.uniq(groupNames)
        _.uniq(groupIntVals)

    processAllGroupFieldModels: () ->
      itemGroups = [@repeatGroups, @nonRepeatGroups]
      for itemGroup in itemGroups
        for group in itemGroup
          groupRowModels = group?.rows?.models?.filter (model) => model?.constructor.kls isnt "Group" and model.cid != @model.cid
          @processFieldModels groupRowModels
      @processFieldModels @nonGroups

    processAllNonRepeatFieldModels: (models, nonRepeatFieldModels) ->
      for model in models
        if @isGroup model
          if not model.get('_isRepeat').get('value')?
            @processAllNonRepeatFieldModels model.rows?.models, nonRepeatFieldModels
        else
          nonRepeatFieldModels.push model

    processGetCurrentAndChildModels: (group, currentAndChildModels) ->
      if group.rows?.models?.length > 0
        groupModels = group.rows?.models
        for model in groupModels
          if @isGroup model
            @processGetCurrentAndChildModels model, currentAndChildModels
          else
            currentAndChildModels.push model

    # expandRowSelector: ->
    #   new $rowSelector.RowSelector(el: @$el.find(".survey__row__spacer").get(0), ngScope: @ngScope, spawnedFromView: @).expand()

    render: (opts={})->
      isNewRow = false
      if @model.get('isNewRow') && @model.get('isNewRow').get('value') is true
        isNewRow = true
        delete @model.attributes.isNewRow

        if @model.get('type').get('typeId') isnt 'note'

          itemGroupPrependVal = 'group'
          itemGroupVal = ''

          @processAllModels @ngScope.survey.rows?.models

          @repeatGroupsItemGroupNames = []
          @repeatGroupsIntVals = []
          @nonRepeatGroupsItemGroupNames = []
          @nonRepeatGroupsIntVals = []
          @nonGroupsItemGroupNames = []
          @nonGroupsIntVals = []
          @processAllGroupFieldModels()

          if @isInRepeatGroupUntilRoot @model
            repeatGroup = @getFirstRepeatGroupUntilRoot @model
            repeatGroupRowsModel = repeatGroup.rows?.models.find (model) => model?.constructor.kls isnt "Group" and model.cid != @model.cid and model.attributes[@itemGroupKey].get('value') != ''
            if repeatGroupRowsModel?
              itemGroupVal = repeatGroupRowsModel.attributes[@itemGroupKey].get('value')
            else
              repeatGroupModels = []
              @processGetCurrentAndChildModels repeatGroup, repeatGroupModels

              if repeatGroupModels.length > 0
                repeatGroupModels = repeatGroupModels.filter (model) => 
                  if model.cid == model.cid
                    model
                  else
                    if model.attributes[@itemGroupKey].get('value') isnt ''
                      model
                currentModelIndex = repeatGroupModels.findIndex (model) => model.cid == @model.cid

                if currentModelIndex != -1 # found
                  repeatGroupModelsMiddleOut = arrayMiddleOut repeatGroupModels, currentModelIndex, 'left'
                  for model in repeatGroupModelsMiddleOut[1..]
                    if @itemGroupKey of model.attributes
                      itemGroupName = model.attributes[@itemGroupKey].get('value')
                      if itemGroupName && itemGroupName != ''
                        itemGroupVal = itemGroupName
                        break
              
              if itemGroupVal is ''
                maxIntVal = 0
                allIntVals = _.union(@repeatGroupsIntVals, @nonRepeatGroupsIntVals, @nonGroupsIntVals)
                if allIntVals.length > 0
                  maxIntVal = Math.max.apply null, allIntVals
                  maxIntVal = 0 if isNaN(maxIntVal)
                itemGroupVal = itemGroupPrependVal + (maxIntVal + 1)
          else
            if @nonRepeatGroups.length == 0 and @nonGroups.length == 0
              maxIntVal = 0
              if @repeatGroupsIntVals.length > 0
                maxIntVal = Math.max.apply null, @repeatGroupsIntVals
                maxIntVal = 0 if isNaN(maxIntVal)
              itemGroupVal = itemGroupPrependVal + (maxIntVal + 1)
            else
              if @model.collection?.models?.length > 0
                currentLevelModels = @model.collection?.models.filter (model) => 
                  if model.cid == model.cid
                    model
                  else
                    if model.attributes[@itemGroupKey].get('value') isnt ''
                      model
                currentModelCollectionIndex = currentLevelModels.findIndex (model) => model.cid == @model.cid
                if currentModelCollectionIndex != -1 # found
                  modelCollectionMiddleOut = arrayMiddleOut currentLevelModels, currentModelCollectionIndex, 'left'
                  for model in modelCollectionMiddleOut[1..]
                    if @isGroup(model) and (not model.get('_isRepeat').get('value')?)
                      currentGroupFieldModels = []
                      @processAllNonRepeatFieldModels model.rows?.models, currentGroupFieldModels
                      for fieldModel in currentGroupFieldModels
                        if @itemGroupKey of fieldModel.attributes
                          itemGroupName = fieldModel.attributes[@itemGroupKey].get('value')
                          if itemGroupName && itemGroupName != ''
                            itemGroupVal = itemGroupName
                            break
                      if itemGroupVal != ''
                        break
                    else
                      if @itemGroupKey of model.attributes
                        itemGroupName = model.attributes[@itemGroupKey].get('value')
                        if itemGroupName && itemGroupName != ''
                          itemGroupVal = itemGroupName
                          break

              if itemGroupVal is ''
                groupNames = _.uniq(_.union(@nonGroupsItemGroupNames, @nonRepeatGroupsItemGroupNames))
                if groupNames.length > 0
                  itemGroupVal =  _.first(groupNames)
                else
                  maxIntVal = 0
                  if @repeatGroupsIntVals.length > 0
                    maxIntVal = Math.max.apply null, @repeatGroupsIntVals
                    maxIntVal = 0 if isNaN(maxIntVal)
                  itemGroupVal = itemGroupPrependVal + (maxIntVal + 1)

          @model.attributes[@itemGroupKey].set('value', itemGroupVal)

      if @model.get('type').get('typeId') is 'note'
        @model.attributes['readonly'].set('value', true)

      fixScroll = opts.fixScroll

      if @already_rendered
        return

      if fixScroll
        @$el.height(@$el.height())

      @already_rendered = true

      if @model instanceof $row.RowError
        @_renderError()
      else
        @_renderRow()
        if isNewRow
          @toggleSettings(true)

      @is_expanded = @$card?.hasClass('card--expandedchoices')

      if fixScroll
        @$el.attr('style', '')

      @
    _renderError: ->
      @$el.addClass("xlf-row-view-error")
      atts = $viewUtils.cleanStringify(@model.toJSON())
      @$el.html $viewTemplates.$$render('row.rowErrorView', atts)
      @
    _renderRow: ->
      @$el.html $viewTemplates.$$render('row.xlfRowView', @surveyView)
      @$name = @$('.card__header-name')
      @$label = @$('.card__header-title')
      @$hint = @$('.card__header-hint')
      @$card = @$('.card')
      @$header = @$('.card__header')
      context = {warnings: []}

      @$label.resizable({
        containment: "parent"
      })

      questionType = @model.get('type').get('typeId')
      if (
        $configs.questionParams[questionType] and
        'getParameters' of @model and
        questionType is 'range'
      )
        @paramsView = new $viewParams.ParamsView({
          rowView: @,
          parameters: @model.getParameters(),
          questionType: questionType
        }).render().insertInDOMAfter(@$header)

      if questionType is 'calculate'
        @$hint.hide()
        @$label.prop('placeholder', _t('Label not needed for Calculate questions'))

      if 'getList' of @model and (cl = @model.getList())
        @$card.addClass('card--selectquestion card--expandedchoices')
        @is_expanded = true
        @listView = new $viewChoices.ListView(model: cl, rowView: @).render()

      if @model.getValue('name')?
        @$name.html(@model.getValue('name'))

      @cardSettingsWrap = @$('.card__settings').eq(0)
      @defaultRowDetailParent = @cardSettingsWrap.find('.card__settings__fields--question-options').eq(0)
      for [key, val] in @model.attributesArray() when key in ['label', 'hint', 'type']
        view = new $viewRowDetail.DetailView(model: val, rowView: @)
        view.render().insertInDOM(@)
      if @model.getValue('required')
        @$card.addClass('card--required')
      @

    toggleSettings: (show)->
      if show is undefined
        show = !@_settingsExpanded

      if show and !@_settingsExpanded
        @_expandedRender()
        @$card.addClass('card--expanded-settings')
        @_settingsExpanded = true
      else if !show and @_settingsExpanded
        @$card.removeClass('card--expanded-settings')
        @_cleanupExpandedRender()
        @_settingsExpanded = false
      ``

    _cleanupExpandedRender: ->
      @$('.card__settings').detach()

    clone: (event) =>
      parent = @model._parent
      model = @model
      if @model.get('type').get('typeId') in ['select_one', 'select_multiple']
        model = @model.clone()
      else if @model.get('type').get('typeId') in ['rank', 'score']
        model = @model.clone()

      @model.getSurvey().insert_row.call parent._parent, model, parent.models.indexOf(@model) + 1

    add_row_to_question_library: (evt) =>
      evt.stopPropagation()
      @ngScope?.add_row_to_question_library @model

  class GroupView extends BaseRowView
    className: "survey__row survey__row--group  xlf-row-view xlf-row-view--depr"
    initialize: (opts)->
      @options = opts
      @_shrunk = !!opts.shrunk
      @$el.attr("data-row-id", @model.cid)
      @surveyView = @options.surveyView

    deleteGroup: (evt)=>
      skipConfirm = $(evt.currentTarget).hasClass('js-force-delete-group')
      if skipConfirm or confirm(_t("Are you sure you want to split apart this group?"))
        @_deleteGroup()
      evt.preventDefault()

    _deleteGroup: () =>
      @model.splitApart()
      @model._parent._parent.trigger('remove', @model)
      @surveyView.survey.trigger('change')
      @$el.detach()

    render: ->
      if !@already_rendered
        @$el.html $viewTemplates.row.groupView(@model)
        @$label = @$('.card__header-title')
        @$rows = @$('.group__rows').eq(0)
        @$card = @$('.card')
        @$header = @$('.card__header,.group__header').eq(0)

      @model.rows.each (row)=>
        @getApp().ensureElInView(row, @, @$rows).render()

      if !@already_rendered
        # only render the row details which are necessary for the initial view (ie 'label')
        view = new $viewRowDetail.DetailView(model: @model.get('label'), rowView: @)
        view.render().insertInDOM(@)

      @already_rendered = true
      @

    hasNestedGroups: ->
      _.filter(@model.rows.models, (row) -> row.constructor.key == 'group').length > 0
    _expandedRender: ->
      @$header.after($viewTemplates.row.groupSettingsView())
      @cardSettingsWrap = @$('.card__settings').eq(0)
      @defaultRowDetailParent = @cardSettingsWrap.find('.card__settings__fields--active').eq(0)
      for [key, val] in @model.attributesArray()
        if key in ["name", "_isRepeat", "appearance", "relevant"] or key.match(/^.+::.+/)
          new $viewRowDetail.DetailView(model: val, rowView: @).render().insertInDOM(@)

      @model.on 'remove', (row) =>
        if row.constructor.key == 'group' && !@hasNestedGroups()
          @$('.xlf-dv-appearance').eq(0).show()
      @

  class RowView extends BaseRowView
    _expandedRender: ->
      @$header.after($viewTemplates.row.rowSettingsView())
      @cardSettingsWrap = @$('.card__settings').eq(0)
      @defaultRowDetailParent = @cardSettingsWrap.find('.card__settings__fields--question-options').eq(0)
      questionType = @model.get('type').get('typeId')

      # don't display columns that start with a $
      hiddenFields = ['label', 'hint', 'type', 'select_from_list_name', 'kobo--matrix_list', 'parameters', 'tags', 'bind::oc:contactdata', 'instance::oc:contactdata']
      for [key, val] in @model.attributesArray() when !key.match(/^\$/) and key not in hiddenFields
        if key is 'required'
          if questionType isnt 'note'
            @mandatorySetting = new $viewMandatorySetting.MandatorySettingView({
              model: @model.get('required')
            }).render().insertInDOM(@)
        else
          if questionType is 'select_one_from_file'
            new $viewRowDetail.DetailView(model: val, rowView: @).render().insertInDOM(@)
          else if questionType is 'calculate'
            if key not in ['readonly', 'select_one_from_file_filename']
              new $viewRowDetail.DetailView(model: val, rowView: @).render().insertInDOM(@)
          else if questionType is 'note'
            if key not in ['readonly', 'bind::oc:itemgroup', 'bind::oc:external', 'calculation', 'bind::oc:briefdescription', 'bind::oc:description', 'select_one_from_file_filename', 'default', 'trigger']
              new $viewRowDetail.DetailView(model: val, rowView: @).render().insertInDOM(@)
          else
            if key isnt 'select_one_from_file_filename'
              new $viewRowDetail.DetailView(model: val, rowView: @).render().insertInDOM(@)

      if (
        $configs.questionParams[questionType] and
        'getParameters' of @model and
        questionType isnt 'range'
      )
        if questionType not in ['select_one', 'select_multiple']
          @paramsView = new $viewParams.ParamsView({
            rowView: @,
            parameters: @model.getParameters(),
            questionType: questionType
          }).render().insertInDOM(@)

      return @

    hideMultioptions: ->
      @$card.removeClass('card--expandedchoices')
      @is_expanded = false
    showMultioptions: ->
      @$card.addClass('card--expandedchoices')
      @$card.removeClass('card--expanded-settings')
      @toggleSettings(false)

    toggleMultioptions: ->
      if @is_expanded
        @hideMultioptions()
      else
        @showMultioptions()
        @is_expanded = true
      return

  class KoboMatrixView extends RowView
    className: "survey__row survey__row--kobo-matrix"
    _expandedRender: ->
      super()
      @$('.xlf-dv-required').hide()
      @$("li[data-card-settings-tab-id='validation-criteria']").hide()
      @$("li[data-card-settings-tab-id='skip-logic']").hide()
    _renderRow: ->
      @$el.html $viewTemplates.row.koboMatrixView()
      @matrix = @$('.card__kobomatrix')
      renderKobomatrix(@, @matrix)
      @$label = @$('.card__header-title')
      @$card = @$('.card')
      @$header = @$('.card__header')
      context = {warnings: []}

      for [key, val] in @model.attributesArray() when key is 'label' or key is 'type'
        view = new $viewRowDetail.DetailView(model: val, rowView: @)
        view.render().insertInDOM(@)
      @

  class RankScoreView extends RowView
    _expandedRender: ->
      super()
      @$('.xlf-dv-required').hide()
      @$("li[data-card-settings-tab-id='validation-criteria']").hide()

  class ScoreView extends RankScoreView
    className: "survey__row survey__row--score"
    _renderRow: (args...)->
      super(args)
      while @model._scoreChoices.options.length < 2
        @model._scoreChoices.options.add(label: 'Option')
      score_choices = for sc in @model._scoreChoices.options.models
        autoname = ''
        if sc.get('name') in [undefined, '']
          autoname = $modelUtils.sluggify(sc.get('label'))

        label: sc.get('label')
        name: sc.get('name')
        autoname: autoname
        cid: sc.cid

      if @model._scoreRows.length < 1
        @model._scoreRows.add
          label: _t("Enter your question")
          name: ''

      score_rows = for sr in @model._scoreRows.models
        if sr.get('name') in [undefined, '']
          autoname = $modelUtils.sluggify(sr.get('label'), validXmlTag: true)
        else
          autoname = ''
        label: sr.get('label')
        name: sr.get('name')
        autoname: autoname
        cid: sr.cid

      template_args = {
        score_rows: score_rows
        score_choices: score_choices
      }

      extra_score_contents = $viewTemplates.$$render('row.scoreView', template_args)
      @$('.card--selectquestion__expansion').eq(0).append(extra_score_contents).addClass('js-cancel-select-row')
      $rows = @$('.score__contents--rows').eq(0)
      $choices = @$('.score__contents--choices').eq(0)

      $el = @$el
      offOn = (evtName, selector, callback)->
        $el.off(evtName).on(evtName, selector, callback)

      get_row = (cid)=> @model._scoreRows.get(cid)
      get_choice = (cid)=> @model._scoreChoices.options.get(cid)
      offOn 'click.deletescorerow', '.js-delete-scorerow', (evt)=>
        $et = $(evt.target)
        row_cid = $et.closest('tr').eq(0).data('row-cid')
        @model._scoreRows.remove(get_row(row_cid))
        @already_rendered = false
        @render(fixScroll: true)
      offOn 'click.deletescorecol', '.js-delete-scorecol', (evt)=>
        $et = $(evt.target)
        @model._scoreChoices.options.remove(get_choice($et.closest('th').data('cid')))
        @already_rendered = false
        @render(fixScroll: true)

      offOn 'input.editscorelabel', '.scorelabel__edit', (evt)->
        $et = $(evt.target)
        row_cid = $et.closest('tr').eq(0).data('row-cid')
        get_row(row_cid).set('label', $et.text())

      offOn 'input.namechange', '.scorelabel__name', (evt)=>
        $ect = $(evt.currentTarget)
        row_cid = $ect.closest('tr').eq(0).data('row-cid')
        _inpText = $ect.text()
        _text = $modelUtils.sluggify(_inpText, validXmlTag: true)
        get_row(row_cid).set('name', _text)

        if _text is ''
          $ect.addClass('scorelabel__name--automatic')
        else
          $ect.removeClass('scorelabel__name--automatic')

        $ect.off 'blur'
        $ect.on 'blur', ()->
          if _inpText isnt _text
            $ect.text(_text)
          if _text is ''
            $ect.addClass('scorelabel__name--automatic')
            $ect.closest('td').find('.scorelabel__edit').trigger('keyup')
          else
            $ect.removeClass('scorelabel__name--automatic')

      offOn 'keyup.namekey', '.scorelabel__edit', (evt)=>
        $ect = $(evt.currentTarget)
        $nameWrap = $ect.closest('.scorelabel').find('.scorelabel__name')
        $nameWrap.attr('data-automatic-name', $modelUtils.sluggify($ect.text(), validXmlTag: true))

      offOn 'input.choicechange', '.scorecell__label', (evt)=>
        $et = $(evt.target)
        get_choice($et.closest('th').data('cid')).set('label', $et.text())

      offOn 'input.optvalchange', '.scorecell__name', (evt)=>
        $et = $(evt.target)
        _text = $et.text()
        if _text is ''
          $et.addClass('scorecell__name--automatic')
        else
          $et.removeClass('scorecell__name--automatic')
        get_choice($et.closest('th').eq(0).data('cid')).set('name', _text)

      offOn 'keyup.optlabelchange', '.scorecell__label', (evt)=>
        $ect = $(evt.currentTarget)
        $nameWrap = $ect.closest('.scorecell__col').find('.scorecell__name')
        $nameWrap.attr('data-automatic-name', $modelUtils.sluggify($ect.text()))

      offOn 'blur.choicechange', '.scorecell__label', (evt)=>
        @render()

      offOn 'click.addchoice', '.scorecell--add', (evt)=>
        @already_rendered = false
        @model._scoreChoices.options.add([label: 'Option'])
        @render(fixScroll: true)

      offOn 'click.addrow', '.scorerow--add', (evt)=>
        @already_rendered = false
        @model._scoreRows.add([label: 'Enter your question'])
        @render(fixScroll: true)

  class RankView extends RankScoreView
    className: "survey__row survey__row--rank"
    _renderRow: (args...)->
      super(args)
      template_args = {}
      template_args.rank_constraint_msg = @model.get('kobo--rank-constraint-message')?.get('value')

      min_rank_levels_count = 2
      if @model._rankRows.length > min_rank_levels_count
        min_rank_levels_count = @model._rankRows.length

      while @model._rankLevels.options.length < min_rank_levels_count
        @model._rankLevels.options.add
          label: "Item to be ranked"
          name: ''

      rank_levels = for model in @model._rankLevels.options.models
        _label = model.get('label')
        _name = model.get('name')
        _automatic = $modelUtils.sluggify(_label)

        label: _label
        name: _name
        automatic: _automatic
        set_automatic: _name is ''
        cid: model.cid
      template_args.rank_levels = rank_levels

      while @model._rankRows.length < 1
        @model._rankRows.add
          label: '1st choice'
          name: ''

      rank_rows = for model in @model._rankRows.models
        _label = model.get('label')
        _name = model.get('name')
        _automatic = $modelUtils.sluggify(_label, validXmlTag: true)

        label: _label
        name: _name
        automatic: _automatic
        set_automatic: _name is ''
        cid: model.cid
      template_args.rank_rows = rank_rows
      extra_score_contents = $viewTemplates.$$render('row.rankView', @, template_args)
      @$('.card--selectquestion__expansion').eq(0).append(extra_score_contents).addClass('js-cancel-select-row')
      @editRanks()
    editRanks: ->
      @$([
          '.rank_items__item__label',
          '.rank_items__level__label',
          '.rank_items__constraint_message',
          '.rank_items__name',
        ].join(',')).attr('contenteditable', 'true')
      $el = @$el
      offOn = (evtName, selector, callback)->
        $el.off(evtName).on(evtName, selector, callback)

      get_item = (evt)=>
        parli = $(evt.target).parents('li').eq(0)
        cid = parli.eq(0).data('cid')
        if parli.hasClass('rank_items__level')
          @model._rankLevels.options.get(cid)
        else
          @model._rankRows.get(cid)

      offOn 'click.deleterankcell', '.js-delete-rankcell', (evt)=>
        if $(evt.target).parents('.rank__rows').length is 0
          collection = @model._rankLevels.options
        else
          collection = @model._rankRows
        item = get_item(evt)
        collection.remove(item)
        @already_rendered = false
        @render(fixScroll: true)

      offOn 'input.ranklabelchange1', '.rank_items__item__label', (evt)->
        $ect = $(evt.currentTarget)
        _text = $ect.text()
        _slugtext = $modelUtils.sluggify(_text, validXmlTag: true)
        $riName = $ect.closest('.rank_items__item').find('.rank_items__name')
        $riName.attr('data-automatic-name', _slugtext)
        get_item(evt).set('label', _text)
      offOn 'input.ranklabelchange2', '.rank_items__level__label', (evt)->
        $ect = $(evt.currentTarget)
        _text = $ect.text()
        _slugtext = $modelUtils.sluggify(_text)
        $riName = $ect.closest('.rank_items__level').find('.rank_items__name')
        $riName.attr('data-automatic-name', _slugtext)
        get_item(evt).set('label', _text)
      offOn 'input.ranklabelchange3', '.rank_items__name', (evt)->
        $ect = $(evt.currentTarget)
        _inptext = $ect.text()
        needs_valid_xml = $ect.parents('.rank_items__item').length > 0
        _text = $modelUtils.sluggify(_inptext, validXmlTag: needs_valid_xml)
        $ect.off 'blur'
        $ect.one 'blur', ->
          if _text is ''
            $ect.addClass('rank_items__name--automatic')
          else
            if _inptext isnt _text
              log 'changin'
              $ect.text(_text)
            $ect.removeClass('rank_items__name--automatic')

        get_item(evt).set('name', _text)

      offOn 'focus', '.rank_items__constraint_message--prelim', (evt)->
        $(evt.target).removeClass('rank_items__constraint_message--prelim').empty()
      offOn 'input.ranklabelchange4', '.rank_items__constraint_message', (evt)=>
        rnkKey = 'kobo--rank-constraint-message'
        @model.get(rnkKey).set('value', evt.target.textContent)
      offOn 'click.addrow', '.rank_items__add', (evt)=>
        if $(evt.target).parents('.rank__rows').length is 0
          # add a level
          @model._rankLevels.options.add({label: 'Item', name: ''})
        else
          chz = "1st 2nd 3rd".split(' ')
          # Please don't go up to 21
          ch = if (@model._rankRows.length + 1 > chz.length) then "#{@model._rankRows.length + 1}th" else chz[@model._rankRows.length]
          @model._rankRows.add({label: "#{ch} choice", name: ''})
        @already_rendered = false
        @render(fixScroll: true)

  RowView: RowView
  ScoreView: ScoreView
  KoboMatrixView: KoboMatrixView
  GroupView: GroupView
  RankView: RankView
