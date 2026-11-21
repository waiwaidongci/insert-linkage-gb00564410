import {datasetField, feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {STATUS} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function runRequest(data, spec) {
  const params = Object.fromEntries(spec.params.map(param => [param.key, data.get(param.key)]));
  return {dataset_id:Number(data.get('dataset_id')), name:data.get('name'), params};
}

function stripMergeParams(payload, spec) {
  for (const key of (spec.merge_params || [])) delete payload.params[key];
  return payload;
}

function mergeDatasetField(datasets, primaryId) {
  const choices = datasets.filter(item => String(item.id) !== String(primaryId));
  return el('label', {for:'field-merge_dataset_id', class:'field'},
    el('span', {}, '并入的新批数据批次'),
    el('select', {id:'field-merge_dataset_id', name:'merge_dataset_id'},
      el('option', {value:''}, '不并入新批（仅切树）'),
      choices.map(item => el('option', {value:item.id}, `${item.name} · ${item.row_count} 行`))));
}

export async function runPage(route, refresh) {
  const {spec,datasets,runs} = workspace;
  const scheduling = spec.scheduled && route.scheduling;
  const plans = scheduling ? await workspace.schedules() : [];
  const error = feedback();
  const submit = button(`执行${spec.run}`,{type:'submit',class:'primary','data-testid':'start-run'});
  const plan = button('保存定时计划',{type:'button',class:'primary','data-testid':'save-schedule'});
  const mergeKeys = spec.merge_params || [];
  const baseParams = spec.params.filter(param => !mergeKeys.includes(param.key));
  const mergeParams = spec.params.filter(param => mergeKeys.includes(param.key));
  const primaryId = route.id || datasets[0]?.id;
  const signedGroups = mergeParams.find(param => param.key === 'signed_groups');
  const mergeBox = mergeParams.length && el('fieldset',
    {class:'fields merge-fields', hidden:true, 'data-testid':'merge-fields'},
    el('legend', {}, spec.merge_run || '两厂清单归并'),
    signedGroups && field({...signedGroups, required:false}),
    p('每行写「组编号: 规格编号、规格编号」，组成员须全部来自当前签核底账批次；旧组编号与成员固定。','subtle'));
  const mergeToggle = mergeParams.length && el('div', {class:'merge-toggle'},
    mergeDatasetField(datasets, primaryId),
    p('选择新批后切换为两厂清单归并：新规格只能并入旧组或另开新组，组内任意两规格不超原切线。','subtle'));
  const scheduleFields = scheduling && session.canReview && el('details',{class:'schedule-fields'},
    el('summary',{},'定时站内分发'),
    p('单次执行锁定所选批次。每日计划读取最新导入的批次，报告日逐日推进。通过发布门槛后进入站内收件箱。','subtle'),
    field({key:'recipient',label:'收件团队',type:'string',required:false,max_length:100}),
    field({key:'first_at',label:'首次执行时间（北京时间）',type:'datetime-local',required:false}),
    el('label',{class:'check'},el('input',{name:'repeat_daily',type:'checkbox'}),'每日同一时间运行'), plan);
  const form = el('form',{class:'fields','data-testid':'run-form',
    onSubmit:handler(submit,error,async event=>{
      const data = new FormData(event.target);
      const payload = runRequest(data,spec);
      if (!data.get('merge_dataset_id')) stripMergeParams(payload, spec);
      const item = await workspace.start(payload);
      location.hash = `#results/${item.id}`;
    })}, datasetField(datasets,route.id),field({key:'name',label:'运行名称',type:'string',max_length:120},`样本${spec.run}`),
    el('div',{class:'form-grid'},baseParams.map(param=>field(param))),mergeToggle,mergeBox,
    el('div',{class:'actions'},submit),scheduleFields,error);
  if (mergeBox) {
    const select = form.querySelector('#field-merge_dataset_id');
    select.addEventListener('change', () => { mergeBox.hidden = !select.value; });
    // 切换底账批次时刷新新批可选范围，避免把同一批次选为新批。
    form.querySelector('#dataset-id').addEventListener('change', event => {
      const rebuilt = mergeDatasetField(datasets, event.target.value).querySelector('select');
      rebuilt.addEventListener('change', () => { mergeBox.hidden = !rebuilt.value; });
      select.replaceWith(rebuilt);
    });
  }
  plan.addEventListener('click',handler(plan,error,async()=>{
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    const recipient = data.get('recipient'), start = data.get('first_at');
    if (!recipient?.trim() || !start) throw new Error('请填写收件团队和首次执行时间');
    const payload = runRequest(data,spec);
    if (!data.get('merge_dataset_id')) stripMergeParams(payload, spec);
    await workspace.schedule({...payload,recipient,first_at:`${start}:00+08:00`,repeat_daily:data.has('repeat_daily')});
    await refresh();
  }));
  const runList = table([
    {label:'运行',render:item=>link(item.name,`#results/${item.id}`)},
    {label:'状态',render:item=>pill(STATUS[item.status],item.status)},
    {label:'时间',render:item=>date(item.created_at)},
  ],runs,{caption:'运行记录',empty:'首次运行后将在此显示后台状态。'});
  const scheduleList = scheduling && panel('定时计划',table([
    {key:'name',label:'计划'}, {key:'recipient',label:'收件团队'},
    {label:'下次执行（北京时间）',render:row=>row.enabled ? date(row.next_at) : '—'},
    {key:'next_report_day',label:'下次报告日'},
    {label:'状态',render:row=>row.enabled ? '启用' : '已结束'},
    {label:'操作',render:row=>{
      if (!row.enabled || !session.canReview) return '—';
      const action = button('停止计划',{class:'quiet'});
      action.addEventListener('click',handler(action,error,async()=>{await workspace.disable(row.id);await refresh();}));
      return action;
    }},
  ],plans,{caption:'定时计划',empty:'还未配置定时计划。'}));
  return el('div',{class:'two-col'}, el('div',{class:'wide-panel'},
    panel(spec.run,session.canWrite ? (datasets.length ? form : empty('请先导入数据。','#inputs','前往导入'))
      : p('只读角色可以查看上方业务流程的历史运行。','empty')),
    !scheduling && panel('运行记录',runList),scheduleList),
    el('aside',{class:'sidebar-panel'},panel('计算口径',el('ul',{class:'method-list'},spec.methodology.map(text=>el('li',{},text))))));
}
