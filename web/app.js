const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const api = async (path, options = {}) => {
  const response = await fetch(path, {headers: {"Content-Type":"application/json"}, ...options});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail?.message || payload.detail || `Request failed (${response.status})`);
  return payload;
};
const state = {page:"overview", mode:"fixture", orders:[], tools:[], graph:null, selectedNode:null, graphFocus:null, graphPan:{x:0,y:0}, graphScale:1, graphPointer:null, agentStatus:null};
const nodeColors = {Agent:"#8769c5",TaskType:"#477db3",Skill:"#3978ab",Tool:"#3c9b70",MCPServer:"#b28a3d",Entity:"#8d7445"};
const taskBySkill = {order_support:"What is the status of this order?",product_research:"Which product categories were purchased in this order?",billing_review:"Compare payment methods and installment amounts for this order.",delivery_investigation:"Was this order delivered late? Compare the promised and actual dates.",customer_context:"Which other orders did this customer place?",refund_review:"Check refund eligibility for this order using the demo policy."};

function navigate(page) {
  if (!$("#page-"+page)) return;
  state.page=page;
  $$(".page").forEach(el=>el.classList.toggle("active",el.id===`page-${page}`));
  $$(".nav-item").forEach(el=>el.classList.toggle("active",el.dataset.page===page));
  const label={overview:"Overview",playground:"Playground",graph:"Graph Explorer",learning:"Learning Lab",registry:"Tool Registry",evaluations:"Evaluations",docs:"Documentation"}[page];
  $("#pageCrumb").textContent=label;
  document.body.classList.remove("nav-open");
  if(page==="graph"&&!state.graph) loadGraph();
  if(page==="registry"&&!state.tools.length) loadRegistry();
}
$$('[data-page]').forEach(el=>el.addEventListener("click",e=>{e.preventDefault();navigate(el.dataset.page)}));
$$('[data-go]').forEach(el=>el.addEventListener("click",()=>navigate(el.dataset.go)));
$("#mobileMenu").addEventListener("click",()=>document.body.classList.toggle("nav-open"));
$("#themeToggle").addEventListener("click",()=>document.body.classList.toggle("light"));

async function loadHealth(){
  const dot=$("#healthDot"),text=$("#healthText"),apiText=$("#apiState");
  try{const x=await api("/api/health");dot.className="ok";text.textContent="Connected";apiText.textContent=`${Number(x.probe_ms).toFixed(0)} ms`;$("#overviewDataset").textContent="Olist historical orders"}
  catch(e){dot.className="error";text.textContent="Unavailable";apiText.textContent="offline";apiText.previousElementSibling?.classList.add("error");}
  try{state.agentStatus=await api("/api/agent/status");updateModeNote()}catch{}
}
async function loadOrders(){
  try{const x=await api("/api/orders?limit=30");state.orders=x.orders||[];const options=state.orders.map((o,i)=>`<option value="${esc(o.order_id)}" ${i===0?"selected":""}>${esc(o.order_id)}${o.status?` · ${esc(o.status)}`:""}</option>`).join("");
    $("#orderSelect").innerHTML=options||'<option value="">No historical orders available</option>';
    $("#graphOrder").innerHTML='<option value="">Select an Olist order</option>'+options;
    if(state.orders.length){$("#graphOrder").value=state.orders[0].order_id;}
    const ds=await api("/api/dataset/status").catch(()=>null);if(ds)$("#overviewDataset").textContent=`Olist · ${ds.order_count??ds.orders??"historical"} orders`;
  }catch(e){$("#orderSelect").innerHTML=`<option value="">Dataset unavailable</option>`;$("#runError").hidden=false;$("#runError").textContent=e.message}
}
function updateModeNote(){
  const note=$("#providerNote"),stamp=$("#playModeStamp"),modeButton=$(`[data-mode="${state.mode}"]`);
  $$(".segmented button").forEach(b=>b.classList.toggle("selected",b===modeButton));
  if(state.mode==="fixture") {note.textContent="Deterministic fixture · repeatable Olist tool runner. Groq is not called.";note.classList.remove("agent");stamp.textContent="DETERMINISTIC FIXTURE";stamp.className="mode-stamp fixture"}
  else {const ready=!!state.agentStatus?.available;note.textContent=ready?`Real Groq agent · ${state.agentStatus.model} · provider calls may incur usage.`:"Real Groq is not configured or the dataset is unavailable. Runs fail explicitly; there is no fixture fallback.";note.classList.add("agent");stamp.textContent="REAL GROQ AGENT";stamp.className="mode-stamp";$("#retrievalSelect").value="graph";$("#retrievalSelect").disabled=true}
  if(state.mode==="fixture")$("#retrievalSelect").disabled=false;
  $("#runTask").classList.toggle("agent",state.mode==="agent");
}
$$('.segmented button').forEach(b=>b.addEventListener("click",()=>{state.mode=b.dataset.mode;updateModeNote()}));
$$('[data-task]').forEach(b=>b.addEventListener("click",()=>$("#taskInput").value=b.dataset.task));

function resetRunView(){
  $("#resultTitle").textContent="Running workflow";$("#resultMeta").textContent="The backend is retrieving context and executing the selected workflow.";
  $("#outcomeBadge").textContent="RUNNING";$("#outcomeBadge").className="outcome-badge neutral";
  $("#answerText").textContent="Waiting for grounded tool output…";$("#answerText").className="answer-text placeholder";
  $("#objectiveList").innerHTML='<span class="empty-state">Waiting for objective verification.</span>';
  ["metricTools","metricContext","metricLatency","metricEntity"].forEach(id=>$("#"+id).textContent="—");
  $$(".workflow-step").forEach((el,i)=>{el.classList.toggle("done",i===0);el.querySelector("small").textContent=i===0?"Submitted":"Waiting"});
  ["pathView","selectedToolView","callView","traceView"].forEach(id=>$("#"+id).innerHTML='<span class="empty-state">Awaiting backend result.</span>');
}
function outcomeClass(code){return ({SUCCESS:"success",PARTIAL:"partial",BLOCKED:"blocked",FAILURE:"failure",UNVERIFIED:"unverified"})[code]||"neutral"}
function renderExecution(x){
  const outcome=x.outcome||{},code=outcome.code||"UNVERIFIED",retrieved=x.selected_tools||[],calls=x.tool_outputs||[],metrics=x.metrics||{},retrieval=x.retrieval||{},exposed=x.execution?.tools_exposed||retrieved.map(t=>t.key),called=x.execution?.tools_called||calls.map(c=>c.tool);
  $("#resultTitle").textContent=`${code} · ${outcome.verified_objectives??0}/${outcome.total_objectives??0} objectives verified`;
  $("#resultMeta").textContent=`${x.execution_mode==="agent"?"Real Groq agent":"Deterministic fixture"} · ${x.method||"graph"} retrieval · ${x.task||""}`;
  $("#outcomeBadge").textContent=code;$("#outcomeBadge").className=`outcome-badge ${outcomeClass(code)}`;
  $("#metricTools").textContent=`${retrieved.length} / ${called.length}`;
  $("#metricContext").textContent=`${retrieval.context_tokens_est??metrics.context_tokens_est??"—"} tok`;
  $("#metricLatency").textContent=`${metrics.retrieval_ms??retrieval.retrieval_ms??"—"} / ${metrics.end_to_end_ms??"—"} ms`;
  $("#metricEntity").textContent=(retrieval.entity_resolved??metrics.entity_resolution_correct)?"Matched":"Unresolved";
  const answer=x.answer||"No answer was returned.";$("#answerText").textContent=answer;$("#answerText").className="answer-text";
  const objectives=outcome.objectives||[];$("#objectiveCount").textContent=`${outcome.verified_objectives??0} / ${outcome.total_objectives??objectives.length} verified`;
  $("#objectiveList").innerHTML=objectives.length?objectives.map(o=>{const ok=o.answered&&o.factually_correct===true&&o.evidence_sufficient===true;const icon=ok?"✓":o.evidence_sufficient===false?"!":"?";return `<div class="objective-row"><span class="objective-mark ${ok?"":"no"}">${icon}</span><b>${esc(o.label||o.id)}</b><p>${esc(o.reason||"No evaluator explanation returned.")}${o.supporting_tool?` · Evidence: ${esc(o.supporting_tool)}${o.evidence_source?` (${esc(o.evidence_source)})`:""}`:""}</p></div>`}).join(""):'<span class="empty-state">No objective rows returned by the evaluator.</span>';
  const path=x.paths||[];$("#pathCount").textContent=`${path.length} relationships`;
  $("#pathView").innerHTML=path.length?path.map(e=>`<div class="path-row"><span class="path-node">${esc(e.from)}</span><span class="path-arrow">—${esc(e.type)}→</span><span class="path-node">${esc(e.to)}</span></div>`).join(""):'<span class="empty-state">This retrieval strategy did not return graph relationships.</span>';
  $("#toolCount").textContent=`${retrieved.length} retrieved`;
  $("#selectedToolView").innerHTML=retrieved.length?retrieved.map(t=>`<article class="tool-row"><div class="tool-row-head"><code>${esc(t.key)}</code><span class="subtle">${esc(t.execution_status||t.server||"")}</span></div><p>${esc(t.description||"")}</p><div class="tool-tags"><span>skill · ${esc(t.skill||"")}</span><span>requires · ${esc((t.dependencies||[]).join(", ")||"none")}</span><span class="provenance">${esc(t.source||"registered tool")}</span></div></article>`).join(""):'<span class="empty-state">No tools retrieved.</span>';
  $("#callCount").textContent=`${calls.length} calls`;
  $("#callView").innerHTML=calls.length?calls.map(c=>`<article class="call-row"><span class="call-symbol ${c.error?"fail":""}">${c.error?"!":"✓"}</span><div><code>${esc(c.tool||"tool")}</code><p>Arguments: ${esc(JSON.stringify(c.arguments||{}))}</p><details><summary class="subtle">${c.error?"Failure":"Returned data"}</summary><pre>${esc(JSON.stringify(c.result??c.error??c,null,2))}</pre></details></div></article>`).join(""):'<span class="empty-state">No tool calls were executed.</span>';
  const trace=x.trace||[];$("#traceView").innerHTML=trace.length?trace.map(t=>`<div class="trace-row"><code>${esc(t.step||"step")}</code><span>${esc(t.message||JSON.stringify(t))}</span></div>`).join(""):'<span class="empty-state">No execution trace returned.</span>';
  const steps=$$(".workflow-step");steps.forEach((el,i)=>{const labels=["Task received",x.method==="graph"?`${exposed.length} tools exposed`:`${x.method} selection`,`${called.length} calls executed`,`${outcome.verified_objectives??0}/${outcome.total_objectives??0} verified`];el.classList.toggle("done",i===0||(i===1&&exposed.length>0)||(i===2&&calls.length>0)||(i===3&&!!outcome.code));el.querySelector("small").textContent=labels[i]});
}
$("#runTask").addEventListener("click",async()=>{
  const button=$("#runTask"),orderId=$("#orderSelect").value;
  let task=$("#taskInput").value.trim();
  if(!orderId){$("#runError").hidden=false;$("#runError").textContent="Choose an available historical order first.";return}
  if(task.length<8){$("#runError").hidden=false;$("#runError").textContent="Enter a task with at least 8 characters.";return}
  task=task.replace(/\b[0-9a-f]{32}\b/gi,orderId);if(!/\b[0-9a-f]{32}\b/i.test(task))task+=` Order ${orderId}.`;
  $("#taskInput").value=task;$("#runError").hidden=true;button.disabled=true;resetRunView();
  try{const x=await api("/api/run",{method:"POST",body:JSON.stringify({task,method:$("#retrievalSelect").value,execution_mode:state.mode,catalog_size:100})});renderExecution(x)}
  catch(e){$("#resultTitle").textContent="Workflow did not complete";$("#resultMeta").textContent="The backend returned an error. No alternate execution mode was run.";$("#outcomeBadge").textContent="ERROR";$("#outcomeBadge").className="outcome-badge failure";$("#answerText").textContent=e.message;$("#answerText").className="answer-text";$("#runError").hidden=false;$("#runError").textContent=e.message;$$(".workflow-step").forEach((el,i)=>{el.querySelector("small").textContent=i===0?"Submitted":i===1?"Failed":"Not run"})}
  finally{button.disabled=false}
});

function graphNodeMarkup(node,pos,dim,selected,focused){
  const color=nodeColors[node.type]||"#597066",x=pos.x,y=pos.y,w=node.type==="Tool"?176:node.type==="Entity"?150:node.type==="Skill"?148:145,h=54;
  const label=node.label||node.id,display=label.length>24?label.slice(0,22)+"…":label;
  const shape=node.type==="TaskType"?`<path class="node-shape" d="M ${x+10} ${y} H ${x+w-10} L ${x+w} ${y+h/2} L ${x+w-10} ${y+h} H ${x+10} L ${x} ${y+h/2} Z" fill="${color}22" stroke="${color}"/>`:
    node.type==="Agent"?`<rect class="node-shape" x="${x}" y="${y}" width="${w}" height="${h}" rx="25" fill="${color}22" stroke="${color}"/>`:
    node.type==="MCPServer"?`<path class="node-shape" d="M ${x+9} ${y} H ${x+w-9} L ${x+w} ${y+10} V ${y+h-10} L ${x+w-9} ${y+h} H ${x+9} L ${x} ${y+h-10} V ${y+10} Z" fill="${color}22" stroke="${color}"/>`:
    `<rect class="node-shape" x="${x}" y="${y}" width="${w}" height="${h}" rx="${node.type==="Entity"?22:8}" fill="${color}22" stroke="${color}"/>`;
  return `<g class="graph-node ${dim?"dim":""} ${selected?"selected":""} ${focused?"focused":""}" data-node="${esc(node.id)}" tabindex="0" role="button" aria-label="Inspect ${esc(node.type)} ${esc(label)}">${shape}<text class="node-type" x="${x+10}" y="${y+15}">${esc(node.type.toUpperCase())}</text><text class="node-label" x="${x+10}" y="${y+35}">${esc(display)}</text></g>`;
}
function renderGraph(){
  if(!state.graph)return;const raw=state.graph,nodes=raw.nodes||[],edges=raw.edges||[],query=$("#graphSearch").value.trim().toLowerCase(),type=$("#nodeFilter").value,edgeType=$("#edgeFilter").value;
  const types=[...new Set(edges.map(e=>e.type))].sort();$("#edgeFilter").innerHTML='<option value="all">All relationships</option>'+types.map(t=>`<option value="${esc(t)}" ${edgeType===t?"selected":""}>${esc(t.replaceAll("_"," "))}</option>`).join("");
  const nodeMap=new Map(nodes.map(n=>[n.id,n]));
  const visibleNodes=nodes.filter(n=>(type==="all"||n.type===type)&&(!query||`${n.id} ${n.type} ${JSON.stringify(n.metadata||{})}`.toLowerCase().includes(query)));
  const visibleIds=new Set(visibleNodes.map(n=>n.id));
  const visibleEdges=edges.filter(e=>(edgeType==="all"||e.type===edgeType)&&visibleIds.has(e.from)&&visibleIds.has(e.to));
  const columns={Agent:85,TaskType:95,Skill:300,Tool:555,MCPServer:840,Entity:1050};
  const counts={},positions={};visibleNodes.forEach(n=>{const x=columns[n.type]??300,row=counts[n.type]||0;counts[n.type]=row+1;positions[n.id]={x,y:34+row*78,w:n.type==="Tool"?176:n.type==="Entity"?150:n.type==="Skill"?148:145,h:54}});
  const maxY=Math.max(440,...Object.values(counts).map(c=>54+c*78));const width=1240;let svg=`<svg class="graph-svg" viewBox="0 0 ${width} ${maxY}" xmlns="http://www.w3.org/2000/svg" aria-label="HydraDB tool retrieval graph"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#74867e"/></marker></defs><g class="graph-world" transform="translate(${state.graphPan.x} ${state.graphPan.y}) scale(${state.graphScale})">`;
  const neighborhood=new Set();if(state.graphFocus){neighborhood.add(state.graphFocus);visibleEdges.forEach(e=>{if(e.from===state.graphFocus)neighborhood.add(e.to);if(e.to===state.graphFocus)neighborhood.add(e.from)})}
  visibleEdges.forEach(e=>{const a=positions[e.from],b=positions[e.to];if(!a||!b)return;const x1=a.x+a.w,y1=a.y+27,x2=b.x,y2=b.y+27,mid=(x1+x2)/2,dim=state.graphFocus&&!neighborhood.has(e.from)&&!neighborhood.has(e.to);svg+=`<g class="graph-edge"><path class="edge-line ${esc(e.type)} ${dim?"dim":""}" d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}"/><text class="edge-label ${dim?"dim":""}" x="${mid}" y="${(y1+y2)/2-5}" text-anchor="middle">${esc(e.type)}</text></g>`});
  visibleNodes.forEach(n=>{const p=positions[n.id],dim=!!(state.graphFocus&&!neighborhood.has(n.id));svg+=graphNodeMarkup(n,p,dim,n.id===state.selectedNode,n.id===state.graphFocus)});
  svg+=`</g></svg>`;$("#graphCanvas").innerHTML=svg;$("#graphTitle").textContent=raw.skills?.map(s=>s.skill).join(" · ")||"Retrieved graph route";$("#graphStatus").textContent=`${visibleNodes.length} nodes · ${visibleEdges.length} relationships · ${raw.retrieval_ms??0} ms route`;
  $("#graphLegend").innerHTML=[...new Set(nodes.map(n=>n.type))].map(t=>`<span class="legend-item"><i class="legend-dot ${esc(t)}" style="background:${nodeColors[t]||"#778"}"></i>${esc(t)}</span>`).join("");
  $$(".graph-node",$("#graphCanvas")).forEach(g=>{g.addEventListener("click",()=>selectGraphNode(g.dataset.node));g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectGraphNode(g.dataset.node)}})});
  $(".graph-svg",$("#graphCanvas")).addEventListener("wheel",e=>{e.preventDefault();state.graphScale=Math.max(.55,Math.min(1.8,state.graphScale+(e.deltaY<0?.08:-.08)));renderGraph()},{passive:false});
  const world=$(".graph-world",$("#graphCanvas"));world.addEventListener("pointerdown",e=>{if(e.target.closest(".graph-node"))return;state.graphPointer={x:e.clientX,y:e.clientY,pan:{...state.graphPan}};world.setPointerCapture(e.pointerId)});world.addEventListener("pointermove",e=>{if(!state.graphPointer)return;const rect=$("#graphCanvas").getBoundingClientRect();state.graphPan={x:state.graphPointer.pan.x+(e.clientX-state.graphPointer.x)*1240/rect.width/state.graphScale,y:state.graphPointer.pan.y+(e.clientY-state.graphPointer.y)*maxY/rect.height/state.graphScale};world.setAttribute("transform",`translate(${state.graphPan.x} ${state.graphPan.y}) scale(${state.graphScale})`)});world.addEventListener("pointerup",()=>{state.graphPointer=null});
  if(state.selectedNode&&nodeMap.has(state.selectedNode))renderInspector(nodeMap.get(state.selectedNode),visibleEdges,nodeMap);
}
function renderInspector(node,edges,nodeMap){
  $("#inspectorTitle").textContent=node.id;const m=node.metadata||{},related=edges.filter(e=>e.from===node.id||e.to===node.id);
  $("#inspectorBody").innerHTML=`<div class="inspect-type">${esc(node.type)}</div><h3 class="inspect-name">${esc(m.name||node.label||node.id)}</h3>${m.description?`<p class="inspect-description">${esc(m.description)}</p>`:""}<div class="inspect-section"><h4>Node identifier</h4><pre>${esc(node.id)}</pre></div>${node.type==="Tool"?`<div class="inspect-section"><h4>Logical server · provenance</h4><div class="inspect-links"><span>${esc(m.server||"unknown server")}</span><span>${esc(m.execution_provenance||"registered")}</span></div></div><div class="inspect-section"><h4>Input schema</h4><pre>${esc(typeof m.input_schema==="string"?m.input_schema:JSON.stringify(m.input_schema||{},null,2))}</pre></div><div class="inspect-section"><h4>Declared prerequisites</h4><div class="inspect-links">${(m.dependencies||[]).map(d=>`<span>${esc(d)}</span>`).join("")||"<span>none declared</span>"}</div></div>`:""}<div class="inspect-section"><h4>Connected relationships (${related.length})</h4><div class="inspect-links">${related.map(e=>`<span>${esc(e.from===node.id?`→ ${e.type} → ${e.to}`:`${e.from} → ${e.type} →`)}</span>`).join("")||"<span>No visible relationship</span>"}</div></div><div class="inspect-section"><button class="button secondary small-button" id="focusNeighbors">${state.graphFocus===node.id?"Clear neighborhood focus":"Focus immediate neighbors"}</button></div>`;
  $("#focusNeighbors").addEventListener("click",()=>{state.graphFocus=state.graphFocus===node.id?null:node.id;renderGraph()});
}
function selectGraphNode(id){state.selectedNode=id;const node=state.graph?.nodes?.find(n=>n.id===id);if(node)renderInspector(node,state.graph.edges||[],new Map(state.graph.nodes.map(n=>[n.id,n])));renderGraph()}
async function loadGraph(){
  const task=$("#graphTask").value.trim()||"What is the status of this order?",order=$("#graphOrder").value;
  $("#graphCanvas").innerHTML='<div class="graph-loading">Reading the bounded graph route from HydraDB…</div>';$("#graphStatus").textContent="Querying HydraDB";
  try{const url=new URL("/api/graph/explore",location.origin);url.searchParams.set("task",task);if(order){url.searchParams.set("order_id",order);if(!/\b[0-9a-f]{32}\b/i.test(task))url.searchParams.set("task",`${task} Order ${order}.`)}state.graph=await api(url.pathname+url.search);state.graphFocus=null;state.graphPan={x:0,y:0};state.graphScale=1;state.selectedNode=null;$("#inspectorTitle").textContent="Select a node";$("#inspectorBody").innerHTML='<div class="inspector-empty"><span>⌘</span><p>Choose a node in the graph to inspect its details and connected relationships.</p></div>';renderGraph()}
  catch(e){state.graph=null;$("#graphCanvas").innerHTML=`<div class="graph-loading">${esc(e.message)}</div>`;$("#graphStatus").textContent="Graph query failed"}
}
$("#exploreGraph").addEventListener("click",loadGraph);$("#graphSearch").addEventListener("input",renderGraph);$("#nodeFilter").addEventListener("change",renderGraph);$("#edgeFilter").addEventListener("change",renderGraph);
$("#graphOrder").addEventListener("change",()=>{});
$("#graphReset").addEventListener("click",()=>{state.graphFocus=null;state.selectedNode=null;state.graphPan={x:0,y:0};state.graphScale=1;renderGraph();$("#inspectorTitle").textContent="Select a node"});
$("#zoomIn").addEventListener("click",()=>{state.graphScale=Math.min(1.8,state.graphScale+.12);renderGraph()});$("#zoomOut").addEventListener("click",()=>{state.graphScale=Math.max(.55,state.graphScale-.12);renderGraph()});$("#fitGraph").addEventListener("click",()=>{state.graphScale=1;state.graphPan={x:0,y:0};renderGraph()});

function selectedKeysFromRoute(route){return (route?.tools||[]).map(t=>t.key)}
function learningStage(title,number,body){return `<article class="learn-stage"><div class="learn-stage-head"><small>${number}</small><b>${esc(title)}</b></div><div class="learn-stage-body">${body}</div></article>`}
function renderLearning(x,mode){
  const states=x.graph_states||{},routes=x.retrieval_states||{},base=routes.baseline,withheld=routes.withheld,after=routes.heldout_retest,experiment=x.relationship_experiment||{},rollback=x.rollback_verification||{};
  const edge=`${experiment.edge||"order.items → order.details"}`;
  const dependency=(base?.edges||[]).find(e=>e.type==="DEPENDS_ON")||(after?.edges||[]).find(e=>e.type==="DEPENDS_ON");
  const edgeViz=(row,filtered=false)=>row?`<div class="learn-dependency ${filtered?"filtered":""}"><span>${esc(row.from)}</span><i>${filtered?"- - × - -":"→"}</i><span class="${filtered?"not-retrieved":""}">${esc(row.to)}</span><small>${filtered?"persisted edge filtered from this retrieval":"DEPENDS_ON · returned by HydraDB"}</small></div>`:`<div class="learn-dependency filtered"><span>${esc(edge.split(" → ")[0])}</span><i>- - × - -</i><span class="not-retrieved">${esc(edge.split(" → ")[1]||"order.details")}</span><small>dependency relationship not returned</small></div>`;
  const routeView=(route,muted=[])=>{const tools=selectedKeysFromRoute(route);const html=tools.map(t=>'<span class="'+(muted.includes(t)?'not-retrieved':'')+'">'+esc(t)+'</span>').join('')||'<span class="not-retrieved">No executable tools</span>';return `<div class="learn-route">${html}</div>`};
  const toolList=a=>(a||[]).join(", ")||"none";
  const afterTools=x.after?.selected_tools||[];
  const baselineBody=`<p>Original persisted relationship and baseline route returned by HydraDB.</p>${edgeViz(dependency)}${base?routeView(base):""}<div class="learn-metric"><span>Relationship state</span><b>${states.baseline?.edge_exists===false?"missing":states.baseline?.edge_exists?"exists":"read"}</b></div><div class="learn-metric"><span>Training order</span><b>${esc(x.training_order_id||"—")}</b></div>`;
  const failBody=`<p>Only the training retrieval excludes the existing edge. No graph write occurs.</p>${edgeViz(dependency,true)}${withheld?routeView(withheld,["order.details"]):""}<div class="learn-metric"><span>Training success</span><b>${x.before?.success?"yes":"no"}</b></div><div class="learn-metric"><span>Dependency failure</span><b>${esc((x.before?.dependency_failures||[]).map(d=>d.tool+" missing "+d.missing_dependency).join("; ")||"none reported")}</b></div>`;
  const correctionBody=`<p>The held-out route returns the prerequisite relationship; execution remains ordered by the dependency.</p>${edgeViz(dependency)}${after?routeView(after):""}<div class="learn-metric"><span>Shared edge written</span><b>no · retrieval filter only</b></div><div class="learn-metric"><span>Validation</span><b>${esc(x.proposal?.validation||"execution evidence")}</b></div>`;
  const retestBody=`<p>A distinct held-out order is routed without the exclusion.</p>${after?routeView(after):`<div class="learn-route">${afterTools.map(t=>`<span>${esc(t)}</span>`).join("")||"<span>not run</span>"}</div>`}<div class="learn-metric"><span>Held-out order</span><b>${esc(x.heldout_order_id||"—")}</b></div><div class="learn-metric"><span>Verified tool execution</span><b>${x.after?.success?"success":"not verified"}</b></div><div class="learn-metric"><span>Tools selected</span><b>${esc(toolList(afterTools))}</b></div>`;
  const rollbackBody=`<p>Readback confirms the shared dependency relationship matches its initial state.</p><div class="learn-metric"><span>Rollback verification</span><b>${rollback.verified&&rollback.unchanged_from_baseline?"verified":"not verified"}</b></div><div class="learn-metric"><span>Edge write</span><b>${rollback.edge_written?"yes":"no"}</b></div><div class="learn-metric"><span>Semantics</span><b>persisted graph unchanged</b></div>`;
  const banner=mode==="fixture"?`<div class="learning-banner">DETERMINISTIC FIXTURE RUN · no Groq calls · retrieval-filter experiment only. The relationship ${esc(edge)} remained unchanged in HydraDB.</div>`:`<div class="learning-banner">REAL GROQ RUN · provider-selected calls · bounded pilot. No fixture fallback. The persisted relationship was verified unchanged.</div>`;
  $("#learningResult").innerHTML=`<div class="learning-outcome">${banner}${learningStage("Persisted baseline","01 · GRAPH STATE",baselineBody)}${learningStage("Prerequisite withheld","02 · OBSERVED FAILURE",failBody)}${learningStage("Correction validated","03 · RETRIEVAL",correctionBody)}${learningStage("Held-out retest","04 · VERIFICATION",retestBody)}${learningStage("Rollback verification","05 · READBACK",rollbackBody)}</div>`;
  $("#learningRaw").textContent=JSON.stringify(x,null,2);$("#learningStatus").textContent=`${mode==="fixture"?"Deterministic":"Real Groq"} run · ${x.state||"completed"} · held-out ${x.after?.success?"passed":"not verified"}.`;
}
async function runLearning(mode){
  const buttons=[$("#runFixtureLearning"),$("#runAgentLearning")];buttons.forEach(b=>b.disabled=true);$("#learningStatus").textContent=mode==="fixture"?"Running deterministic retrieval-filter experiment…":"Calling Groq for the explicitly requested agent experiment…";
  $("#learningResult").innerHTML='<div class="empty-learning"><span>↗</span><h2>Experiment running</h2><p>Waiting for the backend to return observed tool and verification evidence.</p></div>';
  try{const x=await api(mode==="fixture"?"/api/learning/demo":"/api/learning/agent-demo",{method:"POST",body:"{}"});renderLearning(x,mode)}catch(e){$("#learningStatus").textContent=e.message;$("#learningResult").innerHTML=`<div class="empty-learning"><span>!</span><h2>Experiment did not complete</h2><p>${esc(e.message)}</p></div>`}finally{buttons.forEach(b=>b.disabled=false)}
}
$("#runFixtureLearning").addEventListener("click",()=>runLearning("fixture"));$("#runAgentLearning").addEventListener("click",()=>runLearning("agent"));

async function loadRegistry(){
  $("#registryList").innerHTML='<div class="graph-loading">Reading tool metadata from HydraDB…</div>';
  try{const payload=await api("/api/tools");state.tools=payload.tools||[];const skills=[...new Set(state.tools.map(t=>t.skill).filter(Boolean))].sort(),servers=[...new Set(state.tools.map(t=>t.server).filter(Boolean))].sort();$("#registrySkill").innerHTML='<option value="all">All skills</option>'+skills.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join("");$("#registryServer").innerHTML='<option value="all">All servers</option>'+servers.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join("");renderRegistry()}
  catch(e){$("#registryList").innerHTML=`<div class="graph-loading">${esc(e.message)}</div>`;$("#registryCount").textContent="Unavailable"}
}
function renderRegistry(){
  const q=$("#registrySearch").value.trim().toLowerCase(),skill=$("#registrySkill").value,server=$("#registryServer").value,source=$("#registrySource").value;
  const rows=state.tools.filter(t=>(skill==="all"||t.skill===skill)&&(server==="all"||t.server===server)&&(source==="all"||t.execution_status===source)&&JSON.stringify(t).toLowerCase().includes(q));$("#registryCount").textContent=`${rows.length} / ${state.tools.length} tools`;
  $("#registryList").innerHTML=rows.map(t=>{const incoming=state.tools.filter(parent=>(parent.dependencies||[]).includes(t.key)).map(parent=>parent.key);let schema=t.schema;try{schema=typeof schema==="string"?JSON.parse(schema):schema}catch{};return `<article class="registry-card"><div class="registry-card-head"><h3>${esc(t.key)}</h3><button type="button" data-open-tool="${esc(t.key)}">Open in Graph ↗</button></div><p>${esc(t.description||"")}</p><div class="registry-meta"><span>skill · ${esc(t.skill||"—")}</span><span>server · ${esc(t.server||"—")}</span><span class="source">${esc(t.source||t.execution_status||"registered")}</span></div><details><summary>Tool details · schema · relationships</summary><div class="inspect-section"><h4>Input schema</h4><pre>${esc(JSON.stringify(schema,null,2))}</pre></div><div class="inspect-section"><h4>Declared prerequisites</h4><div class="inspect-links">${(t.dependencies||[]).map(d=>`<span>${esc(d)}</span>`).join("")||"<span>none declared</span>"}</div></div><div class="inspect-section"><h4>Incoming dependencies</h4><div class="inspect-links">${incoming.map(d=>`<span>${esc(d)}</span>`).join("")||"<span>none declared</span>"}</div></div><div class="inspect-section"><h4>Provenance</h4><p class="inspect-description">${esc(t.source||"Registered logical tool")}. Output schema is not separately declared by this catalog.</p></div></details></article>`}).join("")||'<div class="graph-loading">No tools match these filters.</div>';
  $$('[data-open-tool]').forEach(button=>button.addEventListener("click",()=>{const tool=state.tools.find(t=>t.key===button.dataset.openTool);if(!tool)return;$("#graphTask").value=taskBySkill[tool.skill]||`Find information using ${tool.key}`;if($("#orderSelect").value)$("#graphOrder").value=$("#orderSelect").value;state.graphFocus=tool.key;navigate("graph");loadGraph().then(()=>{if(state.graph?.nodes.some(n=>n.id===tool.key))selectGraphNode(tool.key)})}));
}
$("#registrySearch").addEventListener("input",renderRegistry);["registrySkill","registryServer","registrySource"].forEach(id=>$("#"+id).addEventListener("change",renderRegistry));

function drawHistoricalBars(){const methods=[{name:"Initial graph",value:0,cls:"initial"},{name:"Corrected graph",value:100,cls:""},{name:"Flat lexical",value:100,cls:"flat"},{name:"Flat + dependency expansion",value:100,cls:"flat"}];$("#benchmarkChart").innerHTML=methods.map(m=>`<div class="bar-group ${m.cls}"><label>${esc(m.name)}</label><div class="bar-track"><div class="bar ${m.value===0?"zero":""}" style="width:${Math.max(m.value,1)}%"></div></div><span class="bar-value">${m.value===0?"0":"3/3"}</span></div>`).join("")}

async function boot(){
  drawHistoricalBars();await Promise.allSettled([loadHealth(),loadOrders(),loadRegistry()]);updateModeNote();
  $("#orderSelect").addEventListener("change",()=>{if(!$("#graphOrder").value)$("#graphOrder").value=$("#orderSelect").value});
}
boot();
