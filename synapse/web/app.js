const B=window.location.origin;
let active='overview';
let copyId=0;
let copySlots={};
let deleteId=0;
let deleteSlots={};
let logStreamController=null;
let logReconnectTimer=null;
let logLines=[];
let taskStreamController=null;
let taskReconnectTimer=null;
let agentTagData=[];
let probeResults=[];
const taskState={mode:'direct',detail:null,live:{},approvalGroup:'',auctionSelection:{groupId:'',taskId:'',executor:'',plan:'',timeout:'',session:''},assignments:[{endpoint:'',title:'',plan:'',timeout:''}]};
const filters={memories:{query:'',persona:'',limit:5,results:null},knowledge:{query:'',persona:'',limit:5,results:null},prompt:{name:'my-agent',type:'http_api'},install:{type:'codex_cli'},logs:{level:'',limit:100,paused:false,autoScroll:true}};

function setTheme(t){document.body.dataset.theme=t;localStorage.setItem('s_theme',t);const b=document.getElementById('theme-btn');if(b)b.textContent=t==='light'?'夜间模式':'日间模式'}
function toggleTheme(){setTheme(document.body.dataset.theme==='light'?'dark':'light')}
setTheme(localStorage.getItem('s_theme')||'dark');

document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',()=>{document.querySelectorAll('nav a').forEach(x=>x.classList.remove('active'));a.classList.add('active');active=a.dataset.tab;load()}));


function activateTab(tab){const link=[...document.querySelectorAll('nav a')].find(a=>a.dataset.tab===tab);if(link)link.click()}
function handleAction(control){
  const action=control.dataset.action;
  switch(action){
    case'toggle-theme':return toggleTheme();
    case'logout':return logout();
    case'save-auth':return saveAuthToken();
    case'root':return window.location.assign('/');
    case'refresh-health':return refreshHealth().then(()=>notify('健康检查已刷新'));
    case'copy-slot':return copySlotText(control.dataset.slot||'');
    case'delete-slot':return deleteSlotItem(control.dataset.slot||'');
    case'reload':return load();
    case'activate-tab':return activateTab(control.dataset.tab||'overview');
    case'search-memory':return searchMemory();
    case'search-knowledge':return searchKnowledge();
    case'clear-search':return clearSearch(control.dataset.kind||'');
    case'submit-memory':return submit('memories',writeBody({persona:g('f-persona')}));
    case'submit-skill':return submit('skills',writeBody({name:g('f-name')}));
    case'submit-knowledge':return submit('knowledge',writeBody({title:g('f-title'),persona:g('f-persona')}));
    case'submit-persona':return submit('personas',writeBody({name:g('f-name')}));
    case'submit-prompt':return submit('prompts',writeBody({name:g('f-name')}));
    case'refresh-agent-tags':return refreshAgentTags(control.dataset.agent||'',control.dataset.profile||'');
    case'probe-agents':return runAgentProbe();
    case'copy-target':return copyFrom(control.dataset.target||'');
    case'load-prompt':return loadPrompt();
    case'show-task':return showTaskDetail(control.dataset.task||'');
    case'close-task-detail':taskState.detail=null;return load();
    case'cancel-task':return cancelTask(control.dataset.task||'');
    case'cancel-group':return cancelTaskGroup(control.dataset.group||'');
    case'open-auction-selection':return openAuctionSelection(control.dataset.group||'',control.dataset.task||'',control.dataset.executor||'');
    case'open-team-approval':return openTeamApproval(control.dataset.group||'');
    case'set-task-mode':return setTaskMode(control.dataset.mode||'direct');
    case'submit-direct-task':return submitDirectTask();
    case'submit-auction':return submitAuction();
    case'submit-team-plan':return submitTeamPlan();
    case'submit-auction-selection':return submitAuctionSelection();
    case'add-assignment':return addAssignment();
    case'remove-assignment':return removeAssignment(Number(control.dataset.index||0));
    case'approve-team':return approveTeamAssignments();
    case'refresh-logs':return refreshLogs();
    case'toggle-log-pause':return toggleLogPause();
    case'copy-visible-logs':return copyVisibleLogs();
    case'clear-visible-logs':return clearVisibleLogs();
    case'update-auth':return askToken();
    case'save-config':return saveConfig();
  }
}
function handleChange(control){
  switch(control.dataset.change){
    case'install-type':filters.install.type=control.value;return load();
    case'log-level':filters.logs.level=control.value;return renderLogs();
    case'log-autoscroll':filters.logs.autoScroll=control.checked;return undefined;
  }
}
document.addEventListener('click',event=>{if(!(event.target instanceof Element))return;const control=event.target.closest('[data-action]');if(!control)return;event.preventDefault();Promise.resolve(handleAction(control)).catch(err=>{if(err.message!==AUTH_REQUIRED)notify('操作失败：'+err.message)})});
document.addEventListener('change',event=>{if(!(event.target instanceof HTMLElement)||!event.target.dataset.change)return;Promise.resolve(handleChange(event.target)).catch(err=>{if(err.message!==AUTH_REQUIRED)notify('操作失败：'+err.message)})});

const AUTH_REQUIRED='__AUTH_REQUIRED__';
function token(){return sessionStorage.getItem('s_token')||''}
function authHeaders(){const h={'Content-Type':'application/json'};const k=token();if(k)h.Authorization='Bearer '+k;return h}
function showAuthRequired(msg='需要 API key 才能访问管理功能。'){
  stopLogStream();
  stopTaskStream();
  const m=document.getElementById('main');
  if(!m)return;
  m.innerHTML=`<div class="auth-panel"><h3>需要认证</h3><p>${e(msg)}</p><p>请在服务端 <span class="inline-code">config.yaml</span> 中查看 <span class="inline-code">server.api_key</span>，然后在本页输入。浏览器只会把 key 保存在当前标签页的 sessionStorage 中。</p><input id="auth-token" type="password" autocomplete="off" placeholder="server.api_key"><div class="auth-actions"><button type="button" data-action="save-auth">进入管理页</button><button type="button" class="secondary" data-action="root">回到根路径</button><button type="button" class="secondary" data-action="refresh-health">只检查健康状态</button></div><p class="muted">如果你是部署者且希望公开只读上下文，可配置 <span class="inline-code">server.public_read_context: true</span>；注意这会公开记忆、知识、技能、人设、提示词和 Agent 状态，管理员接口、日志和两个可能触发 embedding 的语义搜索仍然需要认证。</p></div>`;
  const input=document.getElementById('auth-token');
  if(input){input.focus();input.addEventListener('keydown',event=>{if(event.key==='Enter')saveAuthToken()})}
}
function saveAuthToken(){const v=g('auth-token').trim();if(!v){notify('请输入 API key');return}sessionStorage.setItem('s_token',v);notify('已保存到本页会话');load()}
function askToken(msg='需要 API key 才能访问管理功能。'){showAuthRequired(msg);return ''}
async function api(m,p,b){
  const o={method:m,headers:authHeaders()};
  if(b!==undefined)o.body=JSON.stringify(b);
  const r=await fetch(B+p,o);
  if(r.status===401||r.status===403){sessionStorage.removeItem('s_token');showAuthRequired('认证缺失或 API key 不正确。');throw new Error(AUTH_REQUIRED)}
  if(!r.ok){const text=await r.text();let data={};try{data=JSON.parse(text)}catch(_e){}throw new Error(data.detail?.error||data.error||r.statusText||text)}
  return r.json();
}
function e(s){return s===undefined||s===null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function g(id){const el=document.getElementById(id);return el?el.value:''}
function checked(id){const el=document.getElementById(id);return !!(el&&el.checked)}
function notify(msg){const t=document.getElementById('toast');t.textContent=msg;t.className='toast show';clearTimeout(notify._t);notify._t=setTimeout(()=>t.className='toast',2600)}
function logout(){sessionStorage.removeItem('s_token');notify('已清除本页认证');showAuthRequired('已清除认证。需要重新输入 API key 后才能访问管理功能。')}
async function copyText(text){try{await navigator.clipboard.writeText(text);notify('已复制')}catch(_e){notify('复制失败，请手动选中复制')}}
async function copyFrom(id){const el=document.getElementById(id);if(el)copyText(el.textContent||el.value||'')}
function copyButton(text){if(!text)return '';const id='copy-'+(++copyId);copySlots[id]=String(text);return `<button type="button" class="secondary" data-action="copy-slot" data-slot="${id}">复制</button>`}
function copySlotText(id){copyText(copySlots[id]||'')}
function deleteButton(kind,value){const id='delete-'+(++deleteId);deleteSlots[id]={kind,value:String(value)};return `<button type="button" class="danger" data-action="delete-slot" data-slot="${id}">删除</button>`}
function deleteSlotItem(id){const item=deleteSlots[id];if(item)del(item.kind,item.value)}
function safeDate(v){return v?new Date(v).toLocaleString():''}
function safeUnixDate(v){const n=Number(v||0);if(!n)return '';return new Date(n>1000000000000?n:n*1000).toLocaleString()}
function writeBody(extra={}){const body={...extra};const content=g('f-content').trim();const file=g('f-file').trim();if(content)body.content=content;if(file)body.file=file;return body}
function scoreBadge(x){return x.score===undefined?'':`<span class="badge ok">score ${e(x.score)}</span>`}

async function refreshHealth(){
  const dot=document.getElementById('status-dot');
  try{const r=await fetch(B+'/health?check=db');const h=await r.json();dot.className='dot '+(h.status==='ok'&&h.db==='ok'?'online':'warn');return h}catch(_e){dot.className='dot offline';return {status:'offline',db:'error'}}
}

async function load(){
  if(active!=='logs')stopLogStream();
  if(active!=='tasks')stopTaskStream();
  const m=document.getElementById('main');
  m.innerHTML='<div class="empty">加载中...</div>';
  copyId=0;copySlots={};deleteId=0;deleteSlots={};
  refreshHealth();
  try{
    switch(active){
      case'overview':m.innerHTML=await roverview();break;
      case'agents':m.innerHTML=await ragents();break;
      case'tasks':m.innerHTML=await rtasks();startTaskStream();break;
      case'memories':m.innerHTML=await rm();break;
      case'skills':m.innerHTML=await rs();break;
      case'knowledge':m.innerHTML=await rk();break;
      case'personas':m.innerHTML=await rp();break;
      case'settings':m.innerHTML=await rset();break;
      case'logs':m.innerHTML=await rlogs();renderLogs();startLogStream();break;
      case'prompt':m.innerHTML=await rprompt();break;
      case'install':m.innerHTML=await rinstall();break;
    }
  }catch(err){if(err.message!==AUTH_REQUIRED)m.innerHTML=`<div class="empty">错误: ${e(err.message)}</div>`}
}

function actionButtons(kind,id,copy){return `<span>${copyButton(copy)}</span>${deleteButton(kind,id)}`}
function renderMemoryItem(x){return `<div class="item"><div class="name">#${e(x.id)} ${scoreBadge(x)}</div><div class="body">${e(x.content)}</div><div class="meta"><span>${e(safeDate(x.created_at))}</span>${actionButtons('memories',x.id,x.content)}</div></div>`}
function renderKnowledgeItem(x){const title=x.title||String(x.content||'').split('\n')[0]||'知识';return `<div class="item"><div class="name">${e(title)}<span>#${e(x.id)}</span> ${scoreBadge(x)}</div><div class="body">${e(x.content)}</div><div class="meta"><span>${e(safeDate(x.created_at))}</span>${actionButtons('knowledge',x.id,x.content)}</div></div>`}
function renderTextItem(kind,name,content){return `<div class="item"><div class="name">${e(name)}</div><div class="body">${e(content)}</div><div class="meta">${actionButtons(kind,name,content)}</div></div>`}

async function roverview(){
  const [h,d]=await Promise.all([refreshHealth(),api('GET','/context')]);
  const memCount=Array.isArray(d.memories)?d.memories.length:0,skillCount=Array.isArray(d.skills)?d.skills.length:0,knowledgeCount=Array.isArray(d.knowledge)?d.knowledge.length:0,personaCount=Array.isArray(d.personas)?d.personas.length:0,promptCount=Array.isArray(d.prompts)?d.prompts.length:0;
  const agents=Array.isArray(d.agents?.available)?d.agents.available:[];const onlineCount=agents.filter(x=>x.online).length;
  return `<div class="list"><h3>系统概览</h3>
    <div class="grid"><div class="stat"><strong>${memCount}</strong><span>最近记忆</span></div><div class="stat"><strong>${knowledgeCount}</strong><span>最近知识</span></div><div class="stat"><strong>${skillCount}</strong><span>技能</span></div><div class="stat"><strong>${personaCount}</strong><span>人设</span></div><div class="stat"><strong>${promptCount}</strong><span>提示词</span></div><div class="stat"><strong>${onlineCount}/${agents.length}</strong><span>在线 Agent</span></div></div>
    <div class="item"><div class="name">服务状态 <span class="badge ${h.status==='ok'?'ok':'err'}">${e(h.status)}</span></div><div class="body">HTTP: ${e(h.status)}\nDB: ${e(h.db||'未检查')}\n浏览器页面使用 REST 管理功能；Agent/Worker 仍通过 /ws 长连接互调。</div></div>
    <h3 class="section-title">Agent 与协议摘要</h3><div class="item"><pre class="body inline-code plain-pre">${e(d.default_skills||'')}</pre></div>
    <h3 class="section-title">API 端点</h3><div class="item"><pre class="body inline-code plain-pre">GET  /health?check=db       健康检查\nGET  /context               完整上下文\nGET  /context/agents        当前可用 Agent\nGET  /context/prompts       提示词文档\nPOST /context/memory        搜索记忆\nPOST /context/knowledge     搜索知识\nPOST /admin/memory          添加记忆\nPOST /admin/skill           添加技能\nPOST /admin/knowledge       添加知识\nPOST /admin/persona         设置人设\nPOST /admin/prompt          设置提示词\nGET  /install/{type}        安装指南\nGET  /admin/logs            最近日志\nGET  /admin/logs/stream     实时日志\nGET  /connection-prompt     连接提示词\nPOST /connection-prompt     连接提示词</pre></div>
  </div><div class="form"><div class="card"><h3>快速操作</h3><button class="btn" data-action="reload">刷新当前页</button><button class="btn secondary" data-action="activate-tab" data-tab="agents">查看 Agent</button><button class="btn secondary" data-action="activate-tab" data-tab="prompt">生成连接提示词</button><button class="btn secondary" data-action="activate-tab" data-tab="install">查看安装指南</button><p class="note">主题保存在本地浏览器；认证 key 只保存在当前标签页。</p></div></div>`;
}

async function rm(){
  const f=filters.memories;const d=await api('GET','/context');const rows=f.results||d.memories||[];const title=f.results?`搜索结果：${e(f.query)}`:'记忆列表';const items=rows.map(renderMemoryItem).join('')||'<div class="empty">暂无记忆</div>';
  return `<div class="list"><h3>${title}</h3><div class="toolbar"><input id="m-query" placeholder="搜索记忆" value="${e(f.query)}"><input id="m-persona" placeholder="persona" value="${e(f.persona)}"><select id="m-limit"><option ${f.limit==5?'selected':''}>5</option><option ${f.limit==10?'selected':''}>10</option><option ${f.limit==20?'selected':''}>20</option></select><button data-action="search-memory">搜索</button><button class="secondary" data-action="clear-search" data-kind="memories">清空</button></div>${items}</div>
  <div class="form"><div class="card"><h3>添加记忆</h3><label>内容</label><textarea id="f-content" placeholder="记忆内容"></textarea><label>或 data/ 文件路径</label><input id="f-file" placeholder="data/memory.md"><label>Persona</label><input id="f-persona" placeholder="可选"><button class="btn" data-action="submit-memory">添加</button><p class="note">shared 模式下 persona 会由服务端覆盖。</p></div></div>`;
}
async function searchMemory(){const f=filters.memories;f.query=g('m-query').trim();f.persona=g('m-persona').trim();f.limit=parseInt(g('m-limit'))||5;if(!f.query){notify('请输入搜索词');return}f.results=await api('POST','/context/memory',{query:f.query,persona:f.persona,limit:f.limit});load()}
function clearSearch(kind){filters[kind].query='';filters[kind].results=null;load()}

async function rs(){
  const rows=await api('GET','/context/skills?detail=1');const items=rows.map(s=>renderTextItem('skills',s.name,s.content)).join('')||'<div class="empty">暂无技能</div>';
  return `<div class="list"><h3>技能列表</h3>${items}</div><div class="form"><div class="card"><h3>添加技能</h3><label>名称</label><input id="f-name" placeholder="技能名称"><label>内容</label><textarea id="f-content" placeholder="技能内容"></textarea><label>或 data/ 文件路径</label><input id="f-file" placeholder="data/skill.md"><button class="btn" data-action="submit-skill">添加</button></div></div>`;
}

async function rk(){
  const f=filters.knowledge;const d=await api('GET','/context');const rows=f.results||d.knowledge||[];const title=f.results?`搜索结果：${e(f.query)}`:'知识列表';const items=rows.map(renderKnowledgeItem).join('')||'<div class="empty">暂无知识</div>';
  return `<div class="list"><h3>${title}</h3><div class="toolbar"><input id="k-query" placeholder="搜索知识" value="${e(f.query)}"><input id="k-persona" placeholder="persona" value="${e(f.persona)}"><select id="k-limit"><option ${f.limit==5?'selected':''}>5</option><option ${f.limit==10?'selected':''}>10</option><option ${f.limit==20?'selected':''}>20</option></select><button data-action="search-knowledge">搜索</button><button class="secondary" data-action="clear-search" data-kind="knowledge">清空</button></div>${items}</div>
  <div class="form"><div class="card"><h3>添加知识</h3><label>标题</label><input id="f-title" placeholder="标题"><label>内容</label><textarea id="f-content" placeholder="知识内容"></textarea><label>或 data/ 文件路径</label><input id="f-file" placeholder="data/knowledge.md"><label>Persona</label><input id="f-persona" placeholder="可选"><label><input id="f-chunk" type="checkbox" class="inline-checkbox">按 Markdown 分块</label><button class="btn" data-action="submit-knowledge">添加</button></div></div>`;
}
async function searchKnowledge(){const f=filters.knowledge;f.query=g('k-query').trim();f.persona=g('k-persona').trim();f.limit=parseInt(g('k-limit'))||5;if(!f.query){notify('请输入搜索词');return}f.results=await api('POST','/context/knowledge',{query:f.query,persona:f.persona,limit:f.limit});load()}

async function rp(){
  const rows=await api('GET','/context/personas?detail=1');const items=rows.map(p=>renderTextItem('personas',p.name,p.content)).join('')||'<div class="empty">暂无人设</div>';
  return `<div class="list"><h3>人设列表</h3>${items}</div><div class="form"><div class="card"><h3>设置人设</h3><label>名称</label><input id="f-name" placeholder="人设名称"><label>内容</label><textarea id="f-content" placeholder="人设内容"></textarea><label>或 data/ 文件路径</label><input id="f-file" placeholder="data/persona.md"><button class="btn" data-action="submit-persona">保存</button></div></div>`;
}

function generatedTagRecord(agent,profile){return agentTagData.find(x=>x.agent_name===agent&&x.profile===profile)||null}
function agentProfiles(a){const c=a.client||{};const caps=(c.profile_capabilities&&typeof c.profile_capabilities==='object')?c.profile_capabilities:{};const raw=Array.isArray(c.profiles)?c.profiles:Object.keys(caps);const names=[...new Set(raw.map(String))].sort();return names.map(n=>{const p=Object.assign({name:n},caps[n]||{});const generated=generatedTagRecord(a.name,n);if(generated)p.self_reported=generated;return p})}
function agentCallPayload(agentName,profile){profile=profile||{};const payload={target:agentName,plan:'执行任务',timeout:Number(profile.suggested_timeout||profile.timeout||600)};if(profile.name)payload.profile=profile.name;const aliases=Array.isArray(profile.session_aliases)?profile.session_aliases:[];if(profile.session_required)payload.session_id=profile.default_session_alias||aliases[0]||'replace-with-session-alias';return {type:'send',payload}}
function profileHints(p){const hints=Array.isArray(p.hints)?p.hints:[];const out=[];if(p.suggested_timeout||p.timeout)out.push(`timeout ${p.suggested_timeout||p.timeout}s`);if(p.max_output_bytes)out.push(`max ${p.max_output_bytes}B`);if(p.supports_session)out.push('session');if(p.session_required)out.push('requires session');if(hints.includes('avoid_short_timeout'))out.push('avoid 60s');if(p.advisory_safe)out.push('read-only advisory');for(const tag of (Array.isArray(p.tags)?p.tags:[]))out.push(tag);for(const tag of (Array.isArray(p.self_reported?.tags)?p.self_reported.tags:[]))out.push(`self:${tag}`);return [...new Set(out)]}
function renderSelfAssessment(p){const x=p.self_reported;if(!x)return'';const rows=[['优势',x.strengths],['限制',x.limitations],['适合',x.suitable_tasks]].filter(([,items])=>Array.isArray(items)&&items.length);if(!rows.length)return'';return `<div class="self-assessment"><span class="badge warn">self-reported</span>${rows.map(([label,items])=>`<div><strong>${label}</strong> ${e(items.join(' · '))}</div>`).join('')}</div>`}
function renderProfileRow(a,p){const aliases=Array.isArray(p.session_aliases)?p.session_aliases:[];const aliasText=p.supports_session?(aliases.length?`aliases: ${aliases.join(', ')}`:(p.allow_raw_session_id?'raw session id allowed':'session configured by local worker')):'no session';const pills=profileHints(p).map(x=>`<span class="pill">${e(x)}</span>`).join('');const payload=JSON.stringify(agentCallPayload(a.name,p),null,2);const refresh=p.advisory_safe?`<button type="button" class="secondary" data-action="refresh-agent-tags" data-agent="${e(a.name)}" data-profile="${e(p.name)}">自评</button>`:'';return `<div class="profile-row"><div><strong>${e(p.name)}</strong><div class="pills">${pills}</div><div class="muted">${e(aliasText)} · plan ${e(p.plan_delivery||'argv')}</div>${renderSelfAssessment(p)}</div><div class="task-actions">${refresh}${copyButton(payload)}</div></div>`}
function renderAgentItem(a){const profiles=agentProfiles(a);const caps=Array.isArray(a.capabilities)?a.capabilities:[];const client=a.client||{};const badge=a.online?'ok':'warn';const status=a.online?'在线':'配置';const basePayload=JSON.stringify(agentCallPayload(a.name,null),null,2);const profileRows=profiles.length?profiles.map(p=>renderProfileRow(a,p)).join(''):'<div class="muted">未声明 profile 能力。</div>';const capText=caps.length?caps.join(', '):'未声明';const probe=probeResults.find(x=>x.agent===a.name);const probeBadge=probe?`<span class="badge ${probe.ok?'ok':'err'}">${e(probe.ok?`${probe.rtt_ms}ms`:probe.status)}</span>`:'';return `<div class="item"><div class="name">${e(a.name)} <span class="badge ${badge}">${status}</span>${probeBadge}<span>${e(a.source||'unknown')}</span></div><div class="body">type: ${e(a.type||'unknown')}
client: ${e(client.name||'unknown')} ${client.version?`(${e(client.version)})`:''}
protocol: ${e(a.protocol_version||'')}
last_seen: ${e(safeUnixDate(a.last_seen))}
capabilities: ${e(capText)}</div><div class="meta"><span>${profiles.length?`${profiles.length} profiles`:'no profiles'}</span>${copyButton(basePayload)}</div>${profiles.length?`<div class="compact-gap">${profileRows}</div>`:''}</div>`}
function renderProbeResults(){if(!probeResults.length)return '';return `<div class="probe-grid">${probeResults.map(x=>`<div class="probe-result"><strong>${e(x.agent)} <span class="badge ${x.ok?'ok':'err'}">${e(x.ok?'可达':x.status)}</span></strong><span class="muted">${x.ok?`${e(x.rtt_ms)} ms · ${x.busy?'忙碌':'空闲'} · queue ${e(x.queue_depth)}`:''}</span></div>`).join('')}</div>`}
async function ragents(){const tags=await api('GET','/admin/agent-tags');const d=tags.agents||{};agentTagData=Array.isArray(tags.generated)?tags.generated:[];const agents=Array.isArray(d.available)?d.available:[];const online=agents.filter(x=>x.online).length;const items=agents.map(renderAgentItem).join('')||'<div class="empty">暂无 Agent</div>';return `<div class="list"><h3>Agent 管理</h3><div class="grid"><div class="stat"><strong>${online}/${agents.length}</strong><span>在线 Agent</span></div><div class="stat"><strong>${Array.isArray(d.configured)?d.configured.length:0}</strong><span>配置项</span></div><div class="stat"><strong>${Array.isArray(d.online)?d.online.length:0}</strong><span>WebSocket</span></div></div>${renderProbeResults()}${items}<h3 class="section-title">原始数据</h3><pre id="agents-json" class="inline-code raw-json">${e(JSON.stringify(d,null,2))}</pre></div><div class="form"><div class="card"><h3>连接检查</h3><button class="btn" data-action="probe-agents">广播探测</button><button class="btn secondary" data-action="reload">刷新</button><button class="btn secondary" data-action="copy-target" data-target="agents-json">复制原始 JSON</button><p class="note">连通性探测不调用 LLM；self 标签是 Agent 的自述，不作为权限依据。</p></div></div>`}
async function runAgentProbe(){const d=await api('POST','/admin/agents/probe',{targets:[],timeout:3});probeResults=Array.isArray(d.results)?d.results:[];notify('连接检查完成');load()}
async function refreshAgentTags(agent,profile){const d=await api('POST','/admin/agent-tags/refresh',{agent,profile});notify(`已创建自评任务 ${d.task_id}`);document.querySelector('[data-tab=tasks]').click()}


async function rprompt(){
  const f=filters.prompt;const [r,docs]=await Promise.all([api('GET',`/connection-prompt?agent_name=${encodeURIComponent(f.name)}&agent_type=${encodeURIComponent(f.type)}`),api('GET','/context/prompts?detail=1')]);
  const items=docs.map(p=>renderTextItem('prompts',p.name,p.content)).join('')||'<div class="empty">暂无提示词文档</div>';
  return `<div class="list"><h3>连接提示词</h3><pre id="config-preview">${e(r)}</pre><h3 class="section-title">提示词文档</h3>${items}</div><div class="form"><div class="card"><h3>参数</h3><label>Agent 名称</label><input id="prompt-name" value="${e(f.name)}"><label>类型</label><select id="prompt-type"><option value="generic" ${f.type==='generic'?'selected':''}>Generic</option><option value="http_api" ${f.type==='http_api'?'selected':''}>HTTP API</option><option value="profile_worker" ${f.type==='profile_worker'?'selected':''}>Profile Worker</option><option value="subprocess_worker" ${f.type==='subprocess_worker'?'selected':''}>Subprocess Worker</option><option value="codex_cli" ${f.type==='codex_cli'?'selected':''}>Codex CLI</option></select><button class="btn" data-action="load-prompt">重新生成</button><button class="btn secondary" data-action="copy-target" data-target="config-preview">复制提示词</button><p class="note">参数只影响本次生成的连接提示词标题、自我标识和示例说明，不会注册或修改 Agent；在线列表以 /context/agents 为准。</p></div><div class="card"><h3>保存提示词文档</h3><label>名称</label><input id="f-name" placeholder="usage-rule"><label>内容</label><textarea id="f-content" placeholder="提示词文档内容"></textarea><label>或 data/ 文件路径</label><input id="f-file" placeholder="data/prompt.md"><button class="btn" data-action="submit-prompt">保存</button><p class="note">读取：GET /context/prompts?name=usage-rule。</p></div></div>`;
}
async function loadPrompt(){filters.prompt.name=g('prompt-name')||'my-agent';filters.prompt.type=g('prompt-type')||'http_api';const r=await api('POST','/connection-prompt',{agent_name:filters.prompt.name,agent_type:filters.prompt.type});document.getElementById('config-preview').textContent=r;notify('已重新生成')}

async function rinstall(){
  const t=filters.install.type;const r=await api('GET','/install/'+encodeURIComponent(t));const guide=r.guide||{};const body=JSON.stringify(guide,null,2);
  return `<div class="list"><h3>安装指南</h3><div class="item"><div class="name">${e(r.agent_type)}</div><div class="body"><pre id="install-preview" class="inline-code plain-pre">${e(body)}</pre></div></div></div><div class="form"><div class="card"><h3>类型</h3><label>Agent 类型</label><select id="install-type" data-change="install-type"><option value="profile_worker" ${t==='profile_worker'?'selected':''}>Profile Worker</option><option value="subprocess_worker" ${t==='subprocess_worker'?'selected':''}>Subprocess Worker</option><option value="codex_cli" ${t==='codex_cli'?'selected':''}>Codex CLI</option><option value="claude_code" ${t==='claude_code'?'selected':''}>Claude Code</option><option value="astrbot_http" ${t==='astrbot_http'?'selected':''}>AstrBot HTTP</option></select><button class="btn secondary" data-action="copy-target" data-target="install-preview">复制 JSON</button><p class="note">真实密钥只放在本地配置或环境变量。</p></div></div>`;
}


function endpointKey(agent,profile=''){return encodeURIComponent(agent+'\n'+profile)}
function parseEndpoint(value){try{const [agent,profile='']=decodeURIComponent(value||'').split('\n');return {agent,profile}}catch(_e){return {agent:'',profile:''}}}
function taskEndpoints(agents,advisoryOnly=false){const out=[];for(const a of agents){if(a.type==='http_api'&&a.source!=='ws'){if(!advisoryOnly)out.push({agent:a.name,profile:'',label:a.name+' / HTTP',advisory:false});continue}if(!a.online)continue;const profiles=agentProfiles(a);if(!profiles.length){if(!advisoryOnly)out.push({agent:a.name,profile:'',label:a.name,advisory:false});continue}for(const p of profiles){if(!advisoryOnly||p.advisory_safe)out.push({agent:a.name,profile:p.name,label:`${a.name} / ${p.name}`,advisory:!!p.advisory_safe,timeout:Number(p.suggested_timeout||p.timeout||600)})}}return out}
function endpointOptions(endpoints,selected='',multiple=false){if(!endpoints.length)return '<option value="" disabled>无可用端点</option>';return endpoints.map(x=>`<option value="${e(endpointKey(x.agent,x.profile))}" ${multiple?'':(selected===endpointKey(x.agent,x.profile)?'selected':'')}>${e(x.label)}${x.advisory?' · read-only':''}</option>`).join('')}
function statusKind(status){if(status==='COMPLETED')return'ok';if(['ERROR','TIMEOUT','CANCELLED','ABANDONED'].includes(status))return'err';return'warn'}
function terminalStatus(status){return ['COMPLETED','ERROR','TIMEOUT','CANCELLED','ABANDONED'].includes(status)}
function renderTask(task,{group=null}={}){const live=taskState.live[task.id]||'';const output=live||task.result_preview||'';const title=task.title||task.purpose||'任务';const target=`${task.target_agent||''}${task.profile?' / '+task.profile:''}`;const generated=generatedTagRecord(task.target_agent||'',task.profile||'');const taskTags=(Array.isArray(generated?.tags)?generated.tags:[]).map(tag=>`<span class="pill">self:${e(tag)}</span>`).join('');const canCancel=!terminalStatus(task.status);const selectBid=group&&group.mode==='auction'&&group.status==='AWAITING_SELECTION'&&task.purpose==='bid'&&task.status==='COMPLETED';return `<div class="${group?'group-task':'item'}"><div class="task-head"><div><div class="task-title">${e(title)} <span class="badge ${statusKind(task.status)}">${e(task.status)}</span></div><div class="task-target">${e(target)} · ${e(task.purpose||'execute')} · ${e(task.id)}</div>${taskTags?`<div class="task-tags">${taskTags}</div>`:''}</div><div class="task-actions"><button type="button" class="secondary" data-action="show-task" data-task="${e(task.id)}">详情</button>${selectBid?`<button type="button" data-action="open-auction-selection" data-group="${e(group.id)}" data-task="${e(task.id)}" data-executor="${e(endpointKey(task.target_agent,task.profile))}">选择</button>`:''}${canCancel?`<button type="button" class="danger" data-action="cancel-task" data-task="${e(task.id)}">中断</button>`:''}</div></div>${output?`<pre class="task-output" id="live-${e(task.id)}">${e(output)}</pre>`:''}<div class="meta"><span>${e(safeDate(task.created_at))}</span><span>${task.output_truncated?'output truncated':''}</span></div></div>`}
function renderTaskDetail(){const t=taskState.detail;if(!t)return'';return `<div class="detail-band"><div class="task-head"><div><div class="task-title">${e(t.title||t.purpose||'任务')} <span class="badge ${statusKind(t.status)}">${e(t.status)}</span></div><div class="task-target">${e(t.id)} · ${e(t.target_agent)}${t.profile?' / '+e(t.profile):''}</div></div><button type="button" class="secondary" data-action="close-task-detail">关闭</button></div><h3 class="detail-title">需求</h3><pre class="task-output">${e(t.content||'')}</pre><h3 class="detail-title">输出</h3><pre class="task-output">${e(t.result||'尚无输出')}</pre>${t.cancel_reason?`<div class="muted compact-gap">中断理由：${e(t.cancel_reason)}</div>`:''}</div>`}
function renderGroup(group){const canCancel=!terminalStatus(group.status);const approve=group.mode==='team'&&group.status==='AWAITING_APPROVAL';return `<div class="item"><div class="group-header"><div><div class="name">${e(group.title)} <span class="badge ${statusKind(group.status)}">${e(group.status)}</span><span>${e(group.mode)}</span></div><div class="muted">${e(group.id)}</div></div><div class="task-actions">${approve?`<button type="button" data-action="open-team-approval" data-group="${e(group.id)}">批准分工</button>`:''}${canCancel?`<button type="button" class="danger" data-action="cancel-group" data-group="${e(group.id)}">中断组</button>`:''}</div></div><div class="body compact-gap">${e(group.requirement)}</div><div class="group-tasks">${(group.tasks||[]).map(t=>renderTask(t,{group})).join('')||'<div class="muted group-empty">尚无子任务</div>'}</div></div>`}
function aggregateStats(rows){const map={};for(const row of rows){const key=`${row.target_agent}${row.profile?' / '+row.profile:''}`;map[key]??={requested:0,completed:0,failed:0};const n=Number(row.count||0);if(row.outcome==='requested')map[key].requested+=n;else if(row.outcome==='completed')map[key].completed+=n;else map[key].failed+=n}return map}
function renderAgentStats(rows){const stats=aggregateStats(rows);const requested=Object.values(stats).reduce((n,x)=>n+x.requested,0);const completed=Object.values(stats).reduce((n,x)=>n+x.completed,0);const failed=Object.values(stats).reduce((n,x)=>n+x.failed,0);const lines=Object.entries(stats).sort().map(([name,x])=>`<div class="stat-row"><span>${e(name)}</span><span>请求 ${e(x.requested)}</span><span>完成 ${e(x.completed)}</span><span>异常 ${e(x.failed)}</span></div>`).join('');return `<div class="grid"><div class="stat"><strong>${requested}</strong><span>Agent 调用请求</span></div><div class="stat"><strong>${completed}</strong><span>完成</span></div><div class="stat"><strong>${failed}</strong><span>异常/中断</span></div></div>${lines?`<div class="item">${lines}</div>`:''}`}
function taskComposer(agents){const endpoints=taskEndpoints(agents,false);const advisory=taskEndpoints(agents,true);const mode=taskState.mode;if(mode==='approval')return teamApprovalComposer(endpoints);if(mode==='auction-select')return auctionSelectionComposer(endpoints);const tabs=`<div class="segmented"><button type="button" class="${mode==='direct'?'active':''}" data-action="set-task-mode" data-mode="direct">指定执行</button><button type="button" class="${mode==='auction'?'active':''}" data-action="set-task-mode" data-mode="auction">广播竞拍</button><button type="button" class="${mode==='team'?'active':''}" data-action="set-task-mode" data-mode="team">精英团队</button></div>`;if(mode==='auction')return `<div class="card"><h3>发布竞拍</h3>${tabs}<label>标题</label><input id="task-title" maxlength="128"><label>需求</label><textarea id="task-plan"></textarea><label>只读候选端点</label><select id="task-candidates" multiple>${endpointOptions(advisory,'',true)}</select><label>超时（秒）</label><input id="task-timeout" type="number" min="1" max="3600"><button class="btn" data-action="submit-auction">开始竞拍</button><p class="note">每个候选生成独立、可追踪的只读提案；由人工选标后才执行。</p></div>`;if(mode==='team')return `<div class="card"><h3>团队规划</h3>${tabs}<label>标题</label><input id="task-title" maxlength="128"><label>总需求</label><textarea id="task-plan"></textarea><label>只读规划端点</label><select id="task-endpoint">${endpointOptions(advisory)}</select><label>规划超时（秒）</label><input id="task-timeout" type="number" min="1" max="3600"><button class="btn" data-action="submit-team-plan">生成分工</button><p class="note">规划输出必须经过人工批准和重新指定端点，不会自动执行。</p></div>`;return `<div class="card"><h3>新建任务</h3>${tabs}<label>端点</label><select id="task-endpoint">${endpointOptions(endpoints)}</select><label>标题</label><input id="task-title" maxlength="128"><label>任务需求</label><textarea id="task-plan"></textarea><label>Session alias</label><input id="task-session" maxlength="128"><label>超时（秒）</label><input id="task-timeout" type="number" min="1" max="3600"><button class="btn" data-action="submit-direct-task">发布任务</button></div>`}
function auctionSelectionComposer(endpoints){const s=taskState.auctionSelection;return `<div class="card"><h3>确认选标</h3><label>执行端点</label><select id="auction-executor">${endpointOptions(endpoints,s.executor)}</select><label>最终执行需求</label><textarea id="auction-plan" placeholder="留空则使用竞拍原需求">${e(s.plan)}</textarea><label>Session alias</label><input id="auction-session" maxlength="128" value="${e(s.session)}"><label>超时（秒）</label><input id="auction-timeout" type="number" min="1" max="3600" value="${e(s.timeout)}"><button class="btn" type="button" data-action="submit-auction-selection">确认并执行</button><button class="btn secondary" type="button" data-action="set-task-mode" data-mode="auction">取消</button><p class="note">提案端点和执行端点相互独立；这里只会执行人工明确选择的端点。</p></div>`}
function teamApprovalComposer(endpoints){const rows=taskState.assignments.map((a,i)=>`<div class="assignment-row"><div class="assignment-head"><strong>任务 ${i+1}</strong>${taskState.assignments.length>1?`<button type="button" class="danger" title="移除" data-action="remove-assignment" data-index="${i}">×</button>`:''}</div><label>端点</label><select id="assign-endpoint-${i}">${endpointOptions(endpoints,a.endpoint)}</select><label>标题</label><input id="assign-title-${i}" value="${e(a.title)}" maxlength="128"><label>需求</label><textarea id="assign-plan-${i}">${e(a.plan)}</textarea><label>超时（秒）</label><input id="assign-timeout-${i}" type="number" min="1" max="3600" value="${e(a.timeout)}"></div>`).join('');return `<div class="card"><h3>批准团队分工</h3>${rows}<button class="btn secondary" type="button" data-action="add-assignment">添加任务</button><button class="btn" type="button" data-action="approve-team">批准并执行</button><button class="btn secondary" type="button" data-action="set-task-mode" data-mode="team">取消</button></div>`}
async function rtasks(){const [taskData,groupData,tagData,statData]=await Promise.all([api('GET','/admin/tasks?limit=100'),api('GET','/admin/task-groups?limit=30'),api('GET','/admin/agent-tags'),api('GET','/admin/stats/agents?days=30')]);agentTagData=Array.isArray(tagData.generated)?tagData.generated:[];const agents=Array.isArray(tagData.agents?.available)?tagData.agents.available:[];const tasks=Array.isArray(taskData.tasks)?taskData.tasks:[];const groups=Array.isArray(groupData.groups)?groupData.groups:[];const direct=tasks.filter(x=>!x.group_id);return `<div class="list"><h3>Agent 调用统计（30 天）</h3>${renderAgentStats(statData.stats||[])}${renderTaskDetail()}<h3 class="section-title">任务组</h3>${groups.map(renderGroup).join('')||'<div class="empty">暂无任务组</div>'}<h3 class="section-title">直接任务</h3>${direct.map(t=>renderTask(t)).join('')||'<div class="empty">暂无直接任务</div>'}</div><div class="form"><div class="card"><h3>实时状态</h3><p class="muted">任务流：<span id="task-stream-state" class="badge warn">连接中</span></p><button class="btn secondary" data-action="reload">刷新</button></div>${taskComposer(agents)}</div>`}
function setTaskMode(mode){taskState.mode=mode;taskState.approvalGroup='';load()}
function optionalTimeout(id){const value=parseInt(g(id));return Number.isFinite(value)&&value>0?value:undefined}
async function submitDirectTask(){const endpoint=parseEndpoint(g('task-endpoint'));const plan=g('task-plan').trim();if(!endpoint.agent||!plan){notify('请选择端点并填写任务需求');return}const body={agent:endpoint.agent,profile:endpoint.profile,title:g('task-title').trim(),plan,session_alias:g('task-session').trim()};const timeout=optionalTimeout('task-timeout');if(timeout)body.timeout=timeout;const result=await api('POST','/admin/tasks',body);notify(`任务已发布 ${result.task_id}`);load()}
async function submitAuction(){const title=g('task-title').trim(),requirement=g('task-plan').trim();const select=document.getElementById('task-candidates');const candidates=[...(select?.selectedOptions||[])].map(x=>parseEndpoint(x.value));if(!title||!requirement||!candidates.length){notify('标题、需求和候选端点必填');return}const body={title,requirement,candidates};const timeout=optionalTimeout('task-timeout');if(timeout)body.timeout=timeout;const result=await api('POST','/admin/auctions',body);notify(`竞拍已创建 ${result.group_id}`);load()}
async function submitTeamPlan(){const endpoint=parseEndpoint(g('task-endpoint'));const title=g('task-title').trim(),requirement=g('task-plan').trim();if(!endpoint.agent||!title||!requirement){notify('标题、需求和规划端点必填');return}const body={title,requirement,planner:endpoint};const timeout=optionalTimeout('task-timeout');if(timeout)body.timeout=timeout;const result=await api('POST','/admin/teams',body);notify(`团队规划已创建 ${result.group_id}`);load()}
async function showTaskDetail(taskId){taskState.detail=await api('GET','/admin/tasks/'+encodeURIComponent(taskId));load()}
async function cancelTask(taskId){const reason=prompt('中断理由');if(reason===null||!reason.trim())return;await api('POST',`/admin/tasks/${encodeURIComponent(taskId)}/cancel`,{reason:reason.trim()});notify('任务已中断');load()}
async function cancelTaskGroup(groupId){const reason=prompt('中断任务组的理由');if(reason===null||!reason.trim())return;await api('POST',`/admin/task-groups/${encodeURIComponent(groupId)}/cancel`,{reason:reason.trim()});notify('任务组已中断');load()}
function openAuctionSelection(groupId,taskId,executor){taskState.mode='auction-select';taskState.auctionSelection={groupId,taskId,executor,plan:'',timeout:'',session:''};load()}
async function submitAuctionSelection(){const s=taskState.auctionSelection;const executor=parseEndpoint(g('auction-executor'));if(!executor.agent){notify('请选择执行端点');return}const body={bid_task_id:s.taskId,executor,plan:g('auction-plan').trim(),session_alias:g('auction-session').trim()};const timeout=optionalTimeout('auction-timeout');if(timeout)body.timeout=timeout;await api('POST',`/admin/auctions/${encodeURIComponent(s.groupId)}/select`,body);notify('已选标并发布执行任务');taskState.mode='auction';taskState.auctionSelection={groupId:'',taskId:'',executor:'',plan:'',timeout:'',session:''};load()}
function captureAssignments(){taskState.assignments=taskState.assignments.map((a,i)=>({endpoint:g(`assign-endpoint-${i}`),title:g(`assign-title-${i}`),plan:g(`assign-plan-${i}`),timeout:g(`assign-timeout-${i}`)}))}
function openTeamApproval(groupId){taskState.mode='approval';taskState.approvalGroup=groupId;taskState.assignments=[{endpoint:'',title:'',plan:'',timeout:''}];load()}
function addAssignment(){captureAssignments();if(taskState.assignments.length>=8){notify('最多 8 个任务');return}taskState.assignments.push({endpoint:'',title:'',plan:'',timeout:''});load()}
function removeAssignment(index){captureAssignments();taskState.assignments.splice(index,1);load()}
async function approveTeamAssignments(){captureAssignments();const assignments=[];for(const row of taskState.assignments){const endpoint=parseEndpoint(row.endpoint);if(!endpoint.agent||!row.title.trim()||!row.plan.trim()){notify('每项分工都需要端点、标题和需求');return}const item={...endpoint,title:row.title.trim(),plan:row.plan.trim()};const timeout=parseInt(row.timeout);if(timeout>0)item.timeout=timeout;assignments.push(item)}await api('POST',`/admin/teams/${encodeURIComponent(taskState.approvalGroup)}/approve`,{assignments});notify('团队任务已发布');taskState.mode='team';taskState.approvalGroup='';load()}
function stopTaskStream(){if(taskReconnectTimer){clearTimeout(taskReconnectTimer);taskReconnectTimer=null}if(taskStreamController){taskStreamController.abort();taskStreamController=null}}
function setTaskStreamState(text,kind='warn'){const el=document.getElementById('task-stream-state');if(el){el.className='badge '+kind;el.textContent=text}}
function handleTaskEvent(event){if(!event||!event.event)return;if(event.event==='task_chunk'&&event.task_id&&event.text){taskState.live[event.task_id]=(taskState.live[event.task_id]||'')+event.text;if(taskState.live[event.task_id].length>20000)taskState.live[event.task_id]=taskState.live[event.task_id].slice(-20000);const out=document.getElementById('live-'+event.task_id);if(out){out.textContent=taskState.live[event.task_id];out.scrollTop=out.scrollHeight}return}clearTimeout(handleTaskEvent._timer);handleTaskEvent._timer=setTimeout(()=>{if(active==='tasks')load()},350)}
async function startTaskStream(){stopTaskStream();if(active!=='tasks')return;const ctrl=new AbortController();taskStreamController=ctrl;try{const response=await fetch(B+'/admin/tasks/stream',{headers:authHeaders(),signal:ctrl.signal});if(response.status===401||response.status===403){sessionStorage.removeItem('s_token');showAuthRequired('任务流需要管理员 API key。');throw new Error(AUTH_REQUIRED)}if(!response.ok||!response.body)throw new Error(response.statusText||'任务流不可用');setTaskStreamState('已连接','ok');const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const events=buffer.split('\n\n');buffer=events.pop()||'';for(const raw of events){const line=raw.split('\n').find(x=>x.startsWith('data: '));if(!line)continue;try{handleTaskEvent(JSON.parse(line.slice(6)))}catch(_e){}}}}catch(err){if(!ctrl.signal.aborted){setTaskStreamState('已断开','err');if(err.message!==AUTH_REQUIRED)notify('任务流断开：'+err.message)}}finally{if(taskStreamController===ctrl){taskStreamController=null;if(active==='tasks'&&!ctrl.signal.aborted)taskReconnectTimer=setTimeout(()=>{taskReconnectTimer=null;startTaskStream()},3000)}}}


async function rlogs(){
  const f=filters.logs;const d=await api('GET',`/admin/logs?limit=${encodeURIComponent(f.limit)}`);logLines=Array.isArray(d.logs)?d.logs:[];
  return `<div class="list"><h3>实时日志</h3><div class="toolbar"><select id="log-level" data-change="log-level"><option value="" ${f.level===''?'selected':''}>全部级别</option><option value="DEBUG" ${f.level==='DEBUG'?'selected':''}>DEBUG</option><option value="INFO" ${f.level==='INFO'?'selected':''}>INFO</option><option value="WARNING" ${f.level==='WARNING'?'selected':''}>WARNING</option><option value="ERROR" ${f.level==='ERROR'?'selected':''}>ERROR</option></select><input id="log-limit" class="narrow-input" type="number" min="1" max="500" value="${e(f.limit)}"><button data-action="refresh-logs">刷新</button><button class="secondary" data-action="toggle-log-pause" id="log-pause">${f.paused?'继续':'暂停'}</button><button class="secondary" data-action="copy-visible-logs">复制可见</button><button class="secondary" data-action="clear-visible-logs">清空显示</button></div><div id="log-output"></div></div><div class="form"><div class="card"><h3>状态</h3><p class="muted">来源：${e(d.source||'logs/synaptic_lathe.log')}</p><p class="muted">历史文件：${d.exists?'存在':'尚未创建'}</p><p class="muted">实时连接：<span id="log-stream-state" class="badge warn">连接中</span></p><label><input id="log-autoscroll" type="checkbox" class="inline-checkbox" ${f.autoScroll?'checked':''} data-change="log-autoscroll">自动滚动到底部</label><p class="note">日志会脱敏 Bearer token 和 sk-* key；HTTP 仅记录 path，不记录 query、请求体或 header。此页需要管理员认证。</p></div></div>`;
}
function logLevel(x){return String(x.level||'INFO').toUpperCase()}
function logMatches(x){return !filters.logs.level||logLevel(x)===filters.logs.level}
function logMessage(x){return x.msg||x.raw||JSON.stringify(x)}
function logMetaParts(x){const p=[];if(x.ts)p.push(new Date(x.ts).toLocaleString());if(x.logger)p.push(`logger=${x.logger}`);if(x.event)p.push(`event=${x.event}`);if(x.method&&x.path)p.push(`${x.method} ${x.path}`);if(x.status!==undefined&&x.status!==null)p.push(`status=${x.status}`);if(x.duration_ms!==undefined&&x.duration_ms!==null)p.push(`${x.duration_ms}ms`);if(x.client_ip)p.push(`client=${x.client_ip}`);if(x.agent)p.push(`agent=${x.agent}`);if(x.source)p.push(`source=${x.source}`);if(x.target)p.push(`target=${x.target}`);if(x.task_id)p.push(`task=${x.task_id}`);if(x.profile)p.push(`profile=${x.profile}`);if(x.exit_code!==undefined&&x.exit_code!==null)p.push(`exit=${x.exit_code}`);if(x.timeout)p.push(`timeout=${x.timeout}s`);if(x.queued!==undefined)p.push(`queued=${x.queued}`);if(x.output_truncated)p.push('output_truncated');if(x.stderr_truncated)p.push('stderr_truncated');if(x.correlation_id)p.push(`cid=${x.correlation_id}`);return p}
function renderLogEntry(x){const level=logLevel(x);const meta=logMetaParts(x).join(' ');return `<div class="log-line"><span class="log-level ${e(level.toLowerCase())}">${e(level)}</span><span class="log-meta">${e(meta)}</span><span>${e(logMessage(x))}</span></div>`}
function renderLogs(){const out=document.getElementById('log-output');if(!out)return;const rows=logLines.filter(logMatches);out.innerHTML=rows.map(renderLogEntry).join('')||'<div class="empty">暂无日志</div>';if(filters.logs.autoScroll)out.scrollTop=out.scrollHeight}
async function refreshLogs(){filters.logs.limit=Math.max(1,Math.min(500,parseInt(g('log-limit'))||100));const d=await api('GET',`/admin/logs?limit=${encodeURIComponent(filters.logs.limit)}`);logLines=Array.isArray(d.logs)?d.logs:[];renderLogs();startLogStream()}
function appendLog(x){logLines.push(x);if(logLines.length>1000)logLines=logLines.slice(-1000);if(!filters.logs.paused)renderLogs()}
function toggleLogPause(){filters.logs.paused=!filters.logs.paused;const b=document.getElementById('log-pause');if(b)b.textContent=filters.logs.paused?'继续':'暂停';if(!filters.logs.paused)renderLogs()}
function clearVisibleLogs(){logLines=[];renderLogs()}
function copyVisibleLogs(){const rows=logLines.filter(logMatches).map(x=>`${logLevel(x)} ${logMetaParts(x).join(' ')} ${logMessage(x)}`);copyText(rows.join('\n'))}
function stopLogStream(){if(logReconnectTimer){clearTimeout(logReconnectTimer);logReconnectTimer=null}if(logStreamController){logStreamController.abort();logStreamController=null}}
function setLogStreamState(text,kind='warn'){const el=document.getElementById('log-stream-state');if(el){el.className='badge '+kind;el.textContent=text}}
async function startLogStream(){stopLogStream();if(active!=='logs')return;const ctrl=new AbortController();logStreamController=ctrl;try{const r=await fetch(B+'/admin/logs/stream?limit=0',{headers:authHeaders(),signal:ctrl.signal});if(r.status===401||r.status===403){sessionStorage.removeItem('s_token');showAuthRequired('日志流需要管理员 API key。');throw new Error(AUTH_REQUIRED)}if(!r.ok||!r.body)throw new Error(r.statusText||'日志流不可用');setLogStreamState('已连接','ok');const reader=r.body.getReader();const decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const events=buffer.split('\n\n');buffer=events.pop()||'';for(const ev of events){const line=ev.split('\n').find(x=>x.startsWith('data: '));if(!line)continue;try{appendLog(JSON.parse(line.slice(6)))}catch(_e){}}}}catch(err){if(!ctrl.signal.aborted){setLogStreamState('已断开','err');if(err.message!==AUTH_REQUIRED)notify('日志流断开：'+err.message)}}finally{if(logStreamController===ctrl){logStreamController=null;if(active==='logs'&&!ctrl.signal.aborted)logReconnectTimer=setTimeout(()=>{logReconnectTimer=null;startLogStream()},3000)}}}

async function rset(){
  const [h,r]=await Promise.all([refreshHealth(),api('GET','/admin/config')]);const c=r.config;
  return `<div class="list"><h3>服务器</h3><div class="item"><div class="name">健康状态 <span class="badge ${h.status==='ok'?'ok':'err'}">${e(h.status)}</span></div><div class="body">DB: ${e(h.db||'未检查')}</div></div><div class="item"><div class="name">监听地址</div><div class="body">${e(c.server.host)}:${e(String(c.server.port))}</div></div><div class="item"><div class="name">CORS</div><div class="body">${e(JSON.stringify(c.server.cors_origins))}</div></div><h3 class="section-title">记忆</h3><div class="item"><div class="name">隔离模式</div><div class="body">${e(c.memory.scope)}</div></div><div class="item"><div class="name">Embedding 提供商</div><div class="body">${e(c.memory.embedding_provider)}</div></div>${c.memory.embedding_model?`<div class="item"><div class="name">Embedding 模型</div><div class="body">${e(c.memory.embedding_model)}</div></div>`:''}${c.memory.embedding_api_url?`<div class="item"><div class="name">Embedding API</div><div class="body">${e(c.memory.embedding_api_url)}</div></div>`:''}</div>
  <div class="form"><div class="card"><h3>认证</h3><p class="muted">管理员 key 只能在 config.yaml 中修改。admin: ${c.server.api_key?'已设置':'未设置'}；worker: ${c.server.worker_api_key?'已设置':'回退到 admin key'}</p><button class="btn secondary" data-action="update-auth">更新本页认证</button></div><div class="card"><h3>记忆配置</h3><label>隔离模式</label><select id="s-scope"><option value="shared" ${c.memory.scope==='shared'?'selected':''}>共享</option><option value="persona" ${c.memory.scope==='persona'?'selected':''}>人设隔离</option></select><label>Embedding 提供商</label><select id="s-ep"><option value="local" ${c.memory.embedding_provider==='local'?'selected':''}>本地</option><option value="openai" ${c.memory.embedding_provider==='openai'?'selected':''}>OpenAI Compatible</option><option value="gemini" ${c.memory.embedding_provider==='gemini'?'selected':''}>Gemini</option><option value="nvidia" ${c.memory.embedding_provider==='nvidia'?'selected':''}>NVIDIA</option><option value="ollama" ${c.memory.embedding_provider==='ollama'?'selected':''}>Ollama</option></select><label>Embedding 模型</label><input id="s-emodel" value="${e(c.memory.embedding_model||'')}"><label>Embedding API URL</label><input id="s-eurl" value="${e(c.memory.embedding_api_url||'')}"><p class="muted compact-gap">Embedding API Key 在 config.yaml 中修改。当前: ${c.memory.embedding_api_key?'已设置':'未设置'}</p><label>向量维度 (0=默认)</label><input id="s-dim" type="number" value="${c.memory.embedding_dimensions||0}"><label>超时(秒)</label><input id="s-timeout" type="number" value="${c.memory.embedding_timeout||20}"><label><input id="s-trust-env" type="checkbox" class="inline-checkbox" ${c.memory.embedding_trust_env?'checked':''}>继承服务进程 HTTP(S) proxy 环境</label><button class="btn" data-action="save-config">保存设置</button><p class="note">保存成功后立即用于后续请求；本地模型按需加载。</p></div></div>`;
}

async function saveConfig(){const data={memory:{scope:g('s-scope'),embedding_provider:g('s-ep'),embedding_model:g('s-emodel'),embedding_api_url:g('s-eurl'),embedding_dimensions:parseInt(g('s-dim'))||0,embedding_timeout:parseInt(g('s-timeout'))||20,embedding_trust_env:checked('s-trust-env')}};await api('POST','/admin/config',data);notify('已保存到 config.yaml');load()}
async function submit(type,body){
  if((type==='skills'||type==='personas'||type==='prompts')&&!body.name){notify('名称必填');return}
  if(type==='knowledge'&&!body.title){notify('标题必填');return}
  if(!body.content&&!body.file){notify('请填写内容或 data/ 文件路径');return}
  const maps={memories:'/admin/memory',skills:'/admin/skill',knowledge:'/admin/knowledge',personas:'/admin/persona',prompts:'/admin/prompt'};let url=maps[type];if(type==='knowledge'&&checked('f-chunk'))url+='?chunk=true';await api('POST',url,body);filters.memories.results=null;filters.knowledge.results=null;notify('已保存');load();
}
async function del(type,id){if(!confirm('确认删除？'))return;const v=encodeURIComponent(id);const m={memories:`/admin/memory?id=${v}`,skills:`/admin/skill?name=${v}`,knowledge:`/admin/knowledge?id=${v}`,personas:`/admin/persona?name=${v}`,prompts:`/admin/prompt?name=${v}`}[type];if(m){await api('DELETE',m);notify('已删除');load()}}

load();
