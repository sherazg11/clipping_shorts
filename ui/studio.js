
const out=document.querySelector('#notice'), jobs=document.querySelector('#jobs');
const el=(tag,t)=>{const n=document.createElement(tag); if(t!==undefined)n.textContent=t;return n};
async function api(url,opts){const r=await fetch(url,opts), body=await r.json();if(!r.ok)throw Error(typeof body.detail==='string'?body.detail:'Request failed');return body}
let busy=false;
const panels=new Map(), showHistory=new Set();
const page=document.body.dataset.page;
const library=page==='recordings', queue=page==='queue', overview=page==='overview';
const empty=document.querySelector('#empty');
const libraryFilter=document.querySelector('#libraryFilter');
const downloadAll=document.querySelector('#downloadAll');
const scheduleInfo=document.querySelector('#scheduleInfo'), queueNotice=document.querySelector('#queueNotice')||out;
const publishedStates=new Set(['scheduled','upload_requested','delivery_unconfirmed','schedule_file_error','uploaded']);
let lastData=[];
jobs.hidden=!(library||queue);
function updateVisibility(){
    const filter=libraryFilter?.value||'all';
    const byId=new Map(lastData.map(j=>[j.id,j]));
    let visible=0;
    for(const j of lastData){
        const entry=panels.get(j.id);
        if(!entry)continue;
        const session=byId.get(j.parent_id);
        entry.node.hidden=library
            ? (filter!=='all' && ((filter==='fixed45')!==(j.mode==='fixed45'))) ||
              Boolean(session && j.window_no<(session.visible_from||1) && !showHistory.has(session.id))
            : !j.clips.some(c=>publishedStates.has(c.state));
        if(!entry.node.hidden)visible++;
    }
    empty.hidden=visible>0;
    empty.textContent=library?'No projects match this view. Start with Upload video, Import a video, or Twitch Live.':'Nothing is queued or published yet. Add a ready clip from Recordings.';
}
libraryFilter?.addEventListener('change',updateVisibility);
downloadAll?.addEventListener('click',event=>{
    if(downloadAll.getAttribute('aria-disabled')==='true')event.preventDefault();
});
const jsonOptions=body=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
async function action(button, route, opts, onSuccess, status){
    if(busy)return;
    const project=button.closest('#jobs > .panel, #jobs > div > .panel');
    if(project){
        let local=project.querySelector('.project-feedback');
        if(!local){local=el('output');local.className='project-feedback';local.setAttribute('role','status');project.querySelector('.project-head').after(local);}
        status=local;
    }
    button.disabled=true;busy=true;status.textContent='Working…';
    try{
        const message=onSuccess(await api(route,opts));
        busy=false;await refresh(true);
        if(status.isConnected)status.textContent=message;
        else if(project&&(!project.hidden)){
            project.querySelector('.project-head').after(status);status.textContent=message;
        }else out.textContent=message;
    }
    catch(e){status.textContent=e.message}
    finally{busy=false;button.disabled=false}
}
function card(c,j){
    const box=el('article');box.className='clip';const v=el('video');v.controls=true;v.preload='none';v.playsInline=true;v.src='/clips/'+c.id+'?cut='+c.start+'-'+c.end;
    const offset=j.window_start||0, start=captureClock(offset+c.start), end=captureClock(offset+c.end);
    const dl=el('a','↓ Download MP4');dl.className='text-action';dl.href=v.src+'&download=true';
    const states={upload_requested:'Sent to Make — awaiting confirmation',delivery_unconfirmed:'Delivery unconfirmed — check Make history',schedule_file_error:'Scheduled file missing or over the Make size limit'};
    const meta=el('p',`Source ${start}–${end} · ${states[c.state]||c.state.replaceAll('_',' ')}`);meta.className='clip-meta';
    const heading=el('h3',c.title);
    box.append(heading);
    if(c.download_available)box.append(v,meta,dl);
    else box.append(meta,el('p',c.cloud_cleanup==='removed'?'Supabase copy removed after YouTube upload.':'The rendered MP4 is unavailable.'));
    if(c.cloud_cleanup==='pending')box.append(el('p','YouTube upload confirmed. Supabase cleanup is pending; retry only the Make callback.'));
    if(c.slot)box.append(el('p','Dispatched for slot: '+c.slot));
    if(c.state==='uploaded'){const yt=el('a','Open on YouTube');yt.href='https://www.youtube.com/shorts/'+c.video_id;yt.target='_blank';yt.rel='noopener';box.append(el('p'),yt)}
    const route='/api/clips/'+c.id;
    if(c.state==='scheduled'){
        const b=el('button','Remove from schedule');
        const when=c.upload_at?new Date(c.upload_at*1000).toLocaleString()+' (device time)':'Next daily queue slot';
        box.append(el('p',when+(c.upload_privacy?' · '+c.upload_privacy:'')),b);
        b.onclick=()=>action(b,route+'/unschedule',{method:'POST'},()=> 'Clip removed from the queue.',queueNotice);
    }
    if(c.state==='ready'){
        if(j.duration){
            const details=el('details'), form=el('form'), start=el('input'), end=el('input'), btn=el('button','Render trim'), a=el('label','Start in source (seconds)'), b=el('label','End in source (seconds)');
            for(const [input,value] of [[start,c.start],[end,c.end]]){input.type='number';input.step='0.001';input.min='0';input.max=String(Math.floor(j.duration*1000)/1000);input.value=String(Math.floor(value*1000)/1000);input.required=true}
            a.append(start);b.append(end);form.append(a,b,btn);details.append(el('summary','Adjust start and end'),form);box.append(details);
            form.onsubmit=e=>{e.preventDefault();action(btn,route+'/trim',jsonOptions({start:start.valueAsNumber,end:end.valueAsNumber}),()=> 'Trim rendered. Preview it before queueing.',queueNotice)};
        }
        const f=el('form'),title=el('input'),description=el('textarea'),submit=el('button','Upload now (private)');
        const titleLabel=el('label','Clip title'), descriptionLabel=el('label','YouTube description');
        titleLabel.append(title);descriptionLabel.append(description);
        const save=el('button','Save title');save.type='button';save.className='secondary';
        title.type='text';title.value=c.title;title.maxLength=100;title.required=true;title.setAttribute('aria-label','YouTube title');
        description.placeholder='Optional description';description.maxLength=4000;description.setAttribute('aria-label','YouTube description');
        const pick=(label,options)=>{
            const wrap=el('label',label), select=el('select');
            for(const [value,text] of options){const option=el('option',text);option.value=value;select.append(option);}
            wrap.append(select);return [wrap,select];
        };
        const [visibilityLabel,visibility]=pick('YouTube visibility',[['private','Private'],['public','Public']]);
        const [timingLabel,timing]=pick('Upload timing',[['now','Upload now'],['exact','Specific date and time'],['delay','Upload after a delay'],['daily','Next daily queue slot']]);
        const exact=el('input'),exactLabel=el('label','Date and time (your device time zone)');exact.type='datetime-local';exactLabel.append(exact);
        const delay=el('input'),delayLabel=el('label','Delay (minutes)');delay.type='number';delay.min='1';delay.max='525600';delay.step='1';delayLabel.append(delay);
        const update=()=>{
            const isExact=timing.value==='exact', isDelay=timing.value==='delay';
            exactLabel.style.display=isExact?'':'none';exact.disabled=!isExact;exact.required=isExact;
            delayLabel.style.display=isDelay?'':'none';delay.disabled=!isDelay;delay.required=isDelay;
            submit.textContent=(timing.value==='now'?'Upload now':'Schedule upload')+' ('+visibility.value+')';
        };
        timing.onchange=visibility.onchange=update;update();
        const actions=el('div');actions.className='actions';actions.append(submit);
        const editing=el('details');editing.append(el('summary','Title & publishing'));
        f.append(titleLabel,save,descriptionLabel,el('p','YouTube titles automatically start with GTA 6.'),visibilityLabel,timingLabel,exactLabel,delayLabel,actions);editing.append(f);box.append(editing);
        const titleButton=el('button','Generate title from speech'), titleStatus=el('output');titleButton.type='button';
        titleButton.className='secondary';
        const titleActions=el('div');titleActions.className='actions';save.before(titleActions);titleActions.append(save,titleButton);titleActions.after(titleStatus);
        const titleAction=async (suffix,opts)=>{
            if(save.disabled||titleButton.disabled)return;
            save.disabled=titleButton.disabled=true;
            titleStatus.textContent=suffix==='title'?'Transcribing this clip…':'Saving title…';
            try{
                title.value=(await api(route+'/'+suffix,opts)).title;
                heading.textContent=title.value; titleStatus.textContent='Title saved.';
            }catch(e){titleStatus.textContent=e.message;}
            finally{save.disabled=titleButton.disabled=false;}
        };
        save.onclick=()=>{if(title.reportValidity())titleAction('rename',jsonOptions({title:title.value}));};
        titleButton.onclick=()=>titleAction('title',{method:'POST'});
        f.onsubmit=e=>{
            e.preventDefault();
            const immediate=timing.value==='now';
            const body={title:title.value,description:description.value,privacy:visibility.value};
            if(timing.value==='exact'){
                const due=new Date(exact.value);
                if(!Number.isFinite(due.getTime())||due.getTime()<=Date.now()){queueNotice.textContent='Choose a future date and time.';return;}
                body.upload_at=due.toISOString();
            }
            if(timing.value==='delay')body.delay_minutes=delay.valueAsNumber;
            action(submit,route+(immediate?'/publish':'/schedule'),jsonOptions(body),()=>immediate?'Sent to Make. The YouTube link appears after confirmation.':'Upload scheduled. See the YouTube queue for details.',queueNotice);
        };
    }
    return box;
}
function captureClock(value){
    const seconds=Math.max(0,Math.floor(value||0));
    return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
}
async function refresh(force=false){
    try{
        if(!overview&&!library&&!queue)return;
        const [data,info]=await Promise.all([api('/api/jobs'),overview||queue?api('/api/schedule'):null]);
        const count=data.reduce((sum,j)=>sum+j.clips.filter(c=>c.download_available).length,0);
        if(overview){
            document.querySelector('#clipCount').textContent=count;
            document.querySelector('#activeCount').textContent=data.filter(j=>['awaiting_source','importing','recording','queued','processing'].includes(j.state)).length;
            document.querySelector('#queueCount').textContent=info.count;
            return;
        }
        if(queue)scheduleInfo.textContent=`9:00 AM · 10:00 AM · 4:00 PM (${info.timezone}) · ${info.count} waiting · Daily queue default: ${info.privacy}${info.make_ready?'':' · Make settings incomplete'}`;
        if(library){
            downloadAll.setAttribute('aria-disabled',String(!count));
            downloadAll.tabIndex=count?0:-1;
            document.querySelector('#downloadCount').textContent=count?`${count} MP4 ${count===1?'file':'files'} available`:'No MP4s available yet';
        }
        if(busy&&!force)return;
        lastData=data;
        const byId=new Map(data.map(j=>[j.id,j]));
        for(const [id,entry] of panels)if(!byId.has(id)){entry.node.remove();entry.windows?.remove();panels.delete(id);}
        const totals=new Map();
        for(const j of data)if(j.parent_id){const t=totals.get(j.parent_id)||[0,0,0,0];t[0]++;t[1]+=j.clips.length;t[2]+=Number(['queued','processing'].includes(j.state));t[3]+=Number(j.state==='failed');totals.set(j.parent_id,t)}
        for(const j of [...data].reverse()){
            const session=byId.get(j.parent_id);
            if(queue&&!j.clips.some(c=>publishedStates.has(c.state))){
                panels.get(j.id)?.node.remove();panels.delete(j.id);continue;
            }
            let entry=panels.get(j.id);
            if(!entry){const node=el('section');node.className='panel';entry={node,signature:''};panels.set(j.id,entry);
                if(library&&session&&panels.has(session.id)){const parent=panels.get(session.id);if(!parent.windows){parent.windows=el('div');parent.node.after(parent.windows)}parent.windows.append(node)}else jobs.prepend(node);
            }
            const section=entry.node, signature=JSON.stringify([j,totals.get(j.id),showHistory.has(j.id)]);
            if(entry.signature===signature||(!force&&(section.contains(document.activeElement)||section.querySelector('details[open]')||[...section.querySelectorAll('video')].some(v=>!v.paused))))continue;
            entry.signature=signature;
            const states={awaiting_source:'Waiting for source worker…',importing:'Downloading video…',recording:'Capture active · waiting for Twitch or recording',queued:'Source ready · waiting to process',processing:'Transcribing or rendering…',capture_complete:'Recording complete',capture_partial:'Recording ended early',capture_stopped:'Stopped · checkpoint saved',failed:'Capture stopped'};
            let status=j.state==='failed'?j.error:j.state==='ready'?`${j.clips.length} clip(s) generated${['split','fixed45'].includes(j.mode)?' by time':' from speech'}`:(states[j.state]||j.state);
            const isSession=library&&['live','youtube_live','recorded_file'].includes(j.source_kind)&&!j.parent_id&&j.capture_minutes&&j.state!=='ready';
            const streaming=j.source_kind!=='recorded_file', provider=j.source_kind==='youtube_live'?'YouTube':'Twitch';
            if(isSession){
                const [windows,clips,pending,failed]=totals.get(j.id)||[0,0,0,0];
                const active=j.state==='recording';
                const saved=j.recorded_seconds||0;
                const recorded=active?Math.max(saved,j.live_seconds||0):saved;
                const phases={connecting:`Connecting to ${provider}…`,recording:'Recording live',reconnecting:`Reconnecting to ${provider}…`,processing_video:'Processing stored video'};
                const label=active?(j.stop_at?'Stopping · saving checkpoint…':(phases[j.capture_phase]||(streaming?`Connecting to ${provider}…`:'Opening stored video…'))):(states[j.state]||j.state);
                status=`${label} · ${captureClock(recorded)} / ${captureClock(j.capture_minutes*60)} ${streaming?'recorded':'processed'} · ${captureClock(saved)} saved in ${windows} window(s) · ${pending} processing or queued · ${clips} clip(s)${failed?' · '+failed+' window(s) with no preview (see below)':''}${j.error?' · '+j.error:''}`;
            }
            const projectHead=el('div');projectHead.className='project-head';
            const label=el('div',session?'CAPTURE WINDOW':j.mode==='fixed45'?'45-SECOND PROJECT':'PROJECT');label.className='eyebrow';
            projectHead.append(label,el('h2',session?session.name+' · '+j.name:j.name));
            if(library)projectHead.append(el('p',status));
            section.replaceChildren(projectHead);
            if(isSession){
                const cp=el('a','Download checkpoint JSON');cp.href='/api/jobs/'+j.id+'/checkpoint';section.append(cp);
                if(j.checkpoint_start) section.append(el('p',`This run started at saved ${captureClock(j.checkpoint_start)}${streaming?` · ${provider} resumed at the live edge`:''}.`));
                if(j.stop_at)section.append(el('p','Stop requested: '+new Date(j.stop_at*1000).toLocaleString()));
                if((j.visible_from||1)>1){const old=el('button',showHistory.has(j.id)?'Hide earlier previews':'Show earlier previews');old.type='button';old.style.marginLeft='10px';section.append(old);old.onclick=()=>{if(showHistory.has(j.id))showHistory.delete(j.id);else showHistory.add(j.id);refresh(true)};}
                if(['awaiting_source','recording'].includes(j.state)){
                    const stop=el('button','Stop recording');stop.type='button';stop.disabled=Boolean(j.stop_at);section.append(el('p'),stop);
                    stop.onclick=()=>action(stop,'/api/jobs/'+j.id+'/stop',{method:'POST'},()=> 'Stop requested. Saved footage and previews will be kept.',out);
                }else{
                    const form=el('form'), label=el('label','Total target minutes (1–60)'), minutes=el('input');
                    minutes.type='number';minutes.min='1';minutes.max='60';minutes.required=true;minutes.value=j.capture_minutes;
                    const resume=el('button','Continue from '+captureClock(j.recorded_seconds)), restart=el('button','Start again at 00:00');
                    label.append(minutes);form.append(label,resume,' ',restart);section.append(form);
                    form.onsubmit=e=>{e.preventDefault();const btn=e.submitter||resume;action(btn,'/api/jobs/'+j.id+'/continue',jsonOptions({minutes:minutes.valueAsNumber,restart:btn===restart}),()=>btn===restart?'New run queued at 00:00.':'Continuation queued. Earlier previews are hidden by default.',out);};
                }
            }
            const ready=j.clips.filter(c=>c.state==='ready').length;
            if(library&&ready){const b=el('button',`Queue all ${ready} ready clip${ready===1?'':'s'}`);b.style.marginBottom='14px';section.append(b);
                b.onclick=()=>action(b,'/api/jobs/'+j.id+'/schedule',{method:'POST'},r=>`${r.queued} clip(s) added using their saved titles.`,queueNotice);}
            const clips=queue?j.clips.filter(c=>publishedStates.has(c.state)):j.clips;
            if(clips.length){const grid=el('div');grid.className='grid';clips.forEach(c=>grid.append(card(c,j)));section.append(grid);}
        }
        updateVisibility();
    }catch(e){out.textContent=e.message}
}
const uploadForm=document.querySelector('#upload');
if(uploadForm)uploadForm.onsubmit=e=>{e.preventDefault();const f=e.target,b=f.querySelector('button');b.disabled=true;const req=new XMLHttpRequest();req.open('POST','/api/jobs');req.upload.onprogress=p=>{if(p.lengthComputable)out.textContent=`Uploading ${Math.round(p.loaded*100/p.total)}%`};req.onload=()=>{b.disabled=false;if(req.status===202){out.textContent='Video queued. Open Recordings to track progress.';f.elements.file.value='';refresh(true)}else{try{out.textContent=JSON.parse(req.responseText).detail||'Upload failed.'}catch{out.textContent='Upload failed.'}}};req.onerror=()=>{b.disabled=false;out.textContent='Connection lost. Try the upload again.'};req.send(new FormData(f)) };
for(const form of document.querySelectorAll('.source')){
    form.onsubmit=e=>{e.preventDefault();const f=new FormData(form),kind=form.dataset.kind;
        const minutes=Number(f.get('minutes')||2);
        action(form.querySelector('button'),'/api/sources',jsonOptions({kind,url:f.get('url'),mode:f.get('mode'),minutes,youtube_live:f.has('youtube_live'),authorized:f.has('authorized')}),()=>{
            if(kind==='live'){form.elements.authorized.checked=false;return `${minutes}-minute Twitch capture queued. Open Recordings to track progress.`;}
            form.elements.url.value='';form.elements.authorized.checked=false;form.elements.youtube_live.checked=false;return 'Video or live capture queued. Open Recordings to track progress.';
        },out);
    }
}
if(overview||library||queue){
    refresh();setInterval(()=>refresh(),3000);
    window.addEventListener('focus',()=>refresh());
}
