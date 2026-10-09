'use strict';
const $=id=>document.getElementById(id);
let registering=false,me=null,recipient=null,roomId=null,generation=0,polling=false,xhr=null,history=new Map(),more=false;
const key=()=>roomId!==null?'room:'+roomId:recipient!==null?'dm:'+recipient:'general';
const route=()=>roomId!==null?'room='+roomId:recipient!==null?'recipient='+recipient:'';
const withRoute=path=>path+(path.includes('?')?'&':'?')+route();
function notice(s){$('notice').textContent=s;}
async function api(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-LANLink':'1'},body:data===undefined?undefined:JSON.stringify(data)});const result=await r.json();if(!r.ok){const e=new Error(result.error||'Ошибка запроса');e.status=r.status;throw e;}return result;}
function authMode(value){registering=value;$('code-label').hidden=!value;$('code').required=value;$('register-tab').classList.toggle('active',value);$('login-tab').classList.toggle('active',!value);$('auth-submit').textContent=value?'Создать аккаунт →':'Войти в LANLink →';$('password').autocomplete=value?'new-password':'current-password';$('auth-error').textContent='';}
$('login-tab').onclick=()=>authMode(false);$('register-tab').onclick=()=>authMode(true);
$('auth-form').onsubmit=async e=>{e.preventDefault();$('auth-submit').disabled=true;try{await api(registering?'/api/register':'/api/login',{name:$('name').value,password:$('password').value,code:$('code').value});$('password').value='';$('auth').hidden=true;$('app').hidden=false;await poll();}catch(err){$('auth-error').textContent=err.message;}finally{$('auth-submit').disabled=false;}};
function select(peer,room,name){if(peer===recipient&&room===roomId&&document.body.classList.contains('chat-open'))return;recipient=peer;roomId=room;document.body.classList.add('chat-open');document.body.classList.remove('details-open');generation++;history=new Map();more=false;$('messages').replaceChildren();$('older').hidden=true;$('room').textContent=name;$('details-title').textContent=name;$('room-description').textContent=room!==null?'Комната для участников сети':peer===null?'Сообщения и файлы для всей сети':'Личная переписка · файлы доступны только вам двоим';$('clear-search').click();notice('');poll();}
$('general').onclick=()=>select(null,null,'Общий чат');$('back').onclick=()=>{document.body.classList.remove('chat-open','details-open');};$('details-toggle').onclick=()=>document.body.classList.toggle('details-open');$('details-close').onclick=()=>document.body.classList.remove('details-open');
function badge(count){if(!count)return null;const e=document.createElement('span');e.className='unread';e.textContent=String(count>99?'99+':count);return e;}
function updateComposer(now){const blocked=me?.blocked_until>now;for(const id of ['text','send','attach'])$(id).disabled=!!blocked;$('text').placeholder=blocked?'Отправка заблокирована до '+new Date(me.blocked_until*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'Напишите сообщение…';}
function renderSidebar(data){
  const {users,rooms,unread,now}=data;
  $('me').textContent=me.name;
  $('profile-avatar').replaceChildren(LANUI.avatar(me,'avatar-profile'));
  $('remove-avatar').hidden=!me.avatar_file;
  $('admin-badge').hidden=!me.is_admin;
  $('admin-tools').hidden=!me.is_admin;
  $('general').classList.toggle('selected',key()==='general');
  $('general').querySelector('.unread')?.remove();
  const g=badge(unread.general);if(g)$('general').append(g);
  $('rooms').replaceChildren();
  for(const room of rooms){
    const b=document.createElement('button');
    b.className='channel'+(roomId===room.id?' selected':'');
    b.textContent='# '+room.name;
    const mark=badge(unread['room:'+room.id]);if(mark)b.append(mark);
    b.onclick=()=>select(null,room.id,room.name);
    $('rooms').append(b);
  }
  $('users').replaceChildren();
  for(const user of users){
    if(user.id===me.id)continue;
    const row=document.createElement('div');row.className='user-row';
    const b=document.createElement('button');b.className='channel'+(recipient===user.id?' selected':'');
    const dot=document.createElement('span');dot.className='dot'+(now-user.seen<15?' online':'');
    b.append(LANUI.avatar(user,'avatar-small'),dot,document.createTextNode(user.name+(user.is_admin?' · админ':'')));
    const mark=badge(unread['dm:'+user.id]);if(mark)b.append(mark);
    b.onclick=()=>select(user.id,null,user.name);row.append(b);
    if(me.is_admin&&!user.is_admin){
      const action=document.createElement('button');action.className='user-moderation';action.type='button';
      action.textContent=user.blocked_until>now?'Снять блок':'⏱';
      action.title=user.blocked_until>now?'Разблокировать отправку':'Временно запретить отправку';
      action.onclick=()=>moderateUser(user,user.blocked_until>now?'unblock':'block');
      row.append(action);
    }
    $('users').append(row);
  }
}
async function moderateUser(user,action){
  try{
    if(action==='block'){
      const raw=await LANUI.ask({title:'Ограничить отправку',description:'Пользователь: '+user.name,kind:'number',label:'Срок в минутах — от 5 до 60',value:5,min:5,max:60,submit:'Заблокировать',validate:value=>Number.isInteger(Number(value))&&Number(value)>=5&&Number(value)<=60?'':'Введите целое число от 5 до 60.'});
      if(raw===null)return;
      await api('/api/admin/block',{user_id:user.id,minutes:Number(raw)});
    }else{
      const confirmed=await LANUI.ask({title:'Снять блокировку?',description:'Пользователь: '+user.name,kind:'confirm',submit:'Разблокировать'});
      if(!confirmed)return;
      await api('/api/admin/unblock',{user_id:user.id});
    }
    notice(action==='block'?'Пользователь временно заблокирован.':'Блокировка снята.');
    poll();
  }catch(e){notice(e.message);}
}
$('new-room').onclick=async()=>{
  const name=await LANUI.ask({title:'Новая комната',kind:'text',label:'Название комнаты',maxLength:40,submit:'Создать',validate:value=>value.length>=2&&value.length<=40?'':'Введите от 2 до 40 символов.'});
  if(name===null)return;
  try{const room=await api('/api/room/create',{name});select(null,room.id,room.name);}catch(e){notice(e.message);}
};
$('join-room').onclick=async()=>{
  try{
    const rooms=(await api('/api/rooms')).rooms;
    if(!rooms.length){notice('Пока нет комнат. Создайте первую.');return;}
    const chosen=await LANUI.ask({title:'Найти комнату',kind:'select',label:'Доступные комнаты',choices:rooms.map(room=>({value:room.id,label:'# '+room.name+' · '+room.members+' участников'})),submit:'Войти'});
    if(chosen===null)return;
    const room=await api('/api/room/join',{id:Number(chosen)});
    select(null,room.id,room.name);
  }catch(e){notice(e.message);}
};
$('audit-button').onclick=async()=>{try{const rows=(await api('/api/admin/audit')).events;$('search-results').hidden=false;$('clear-search').hidden=false;$('search-results').replaceChildren();const title=document.createElement('b');title.textContent='Последние 100 действий';$('search-results').append(title);for(const r of rows){const p=document.createElement('p');p.textContent=new Date(r.created*1000).toLocaleString('ru-RU')+' · '+r.actor+' · '+r.action+' · '+r.target;$('search-results').append(p);}if(!rows.length)$('search-results').append(document.createTextNode('Действий пока нет.'));}catch(e){notice(e.message);}};
async function showCensorship(){try{const words=(await api('/api/admin/censorship')).terms;const box=$('search-results');box.hidden=false;$('clear-search').hidden=false;box.replaceChildren();const title=document.createElement('b');title.textContent='Цензура · '+words.length+' слов';box.append(title);const help=document.createElement('p');help.textContent='Одно слово; звёздочка в конце охватывает окончания. Нажмите ×, чтобы удалить слово.';box.append(help);const form=document.createElement('form');form.className='filter-form';const input=document.createElement('input');input.placeholder='Добавить слово, например грубость*';input.required=true;input.maxLength=33;const add=document.createElement('button');add.textContent='Добавить';form.append(input,add);form.onsubmit=async e=>{e.preventDefault();try{await api('/api/admin/censorship/add',{word:input.value.trim()});notice('Слово добавлено.');showCensorship();}catch(err){notice(err.message);}};box.append(form);const pills=document.createElement('div');pills.className='filter-words';for(const word of words){const b=document.createElement('button');b.type='button';b.textContent=word+' ×';b.title='Убрать из списка: '+word;b.onclick=async()=>{if(!await LANUI.ask({title:'Убрать слово из цензуры?',description:word,kind:'confirm',submit:'Удалить',danger:true}))return;try{await api('/api/admin/censorship/remove',{word});showCensorship();}catch(err){notice(err.message);}};pills.append(b);}box.append(pills);}catch(e){notice(e.message);}}
$('censor-button').onclick=showCensorship;
$('search-form').onsubmit=async e=>{e.preventDefault();const q=$('search-query').value.trim();if(q.length<2)return;const gen=generation;try{const rows=(await api(withRoute('/api/search?q='+encodeURIComponent(q)))).results;if(gen!==generation)return;$('search-results').hidden=false;$('clear-search').hidden=false;$('search-results').replaceChildren();const title=document.createElement('b');title.textContent='Результаты поиска ('+rows.length+')';$('search-results').append(title);for(const r of rows){const p=document.createElement('p');p.textContent=r.sender_name+' · '+new Date(r.created*1000).toLocaleDateString('ru-RU')+' · '+(r.file_name||r.text);$('search-results').append(p);}if(!rows.length)$('search-results').append(document.createTextNode('Совпадений нет.'));}catch(err){notice(err.message);}};
$('clear-search').onclick=()=>{$('search-results').hidden=true;$('clear-search').hidden=true;$('search-results').replaceChildren();};
function messageNode(m){
  const item=document.createElement('article');
  item.className='message'+(m.sender===me.id?' mine':'');
  const content=document.createElement('div');content.className='message-content';
  const meta=document.createElement('div');meta.className='message-meta';
  meta.textContent=m.sender_name+(m.sender_is_admin?' · админ':'')+' · '+new Date(m.created*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+(m.edited?' · изменено':'');
  const bubble=document.createElement('div');bubble.className='bubble';
  if(m.deleted){bubble.classList.add('deleted-message');bubble.textContent='Сообщение удалено';}
  else if(m.file_id){
    const a=document.createElement('a');a.className='file-link';a.href='/download/'+encodeURIComponent(m.file_id);a.textContent='↓ '+m.file_name;
    const size=document.createElement('div');size.className='file-size';size.textContent=(m.file_size/1024/1024).toFixed(2)+' МБ · Скачать';
    bubble.append(a,size);
  }else bubble.textContent=m.text;
  content.append(meta,bubble);
  const canManage=m.sender===me.id||(recipient===null&&roomId===null&&me.is_admin);
  if(canManage&&!m.deleted){
    const actions=document.createElement('div');actions.className='message-actions';
    if(!m.file_id){
      const edit=document.createElement('button');edit.type='button';edit.textContent='Изменить';
      edit.onclick=async()=>{
        const value=await LANUI.ask({title:'Изменить сообщение',kind:'textarea',label:'Текст сообщения',value:m.text,maxLength:4000,submit:'Сохранить',validate:text=>text.length>=1&&text.length<=4000?'':'Введите сообщение длиной до 4000 символов.'});
        if(value===null)return;
        try{const result=await api('/api/message/edit',{id:m.id,text:value});if(result.censored)notice('Слово из списка цензуры скрыто звёздочками.');poll();}catch(e){notice(e.message);}
      };
      actions.append(edit);
    }
    const remove=document.createElement('button');remove.type='button';remove.textContent='Удалить';
    remove.onclick=async()=>{
      const confirmed=await LANUI.ask({title:'Удалить сообщение?',description:m.file_id?'Вложение также будет удалено.':'Сообщение исчезнет у всех участников беседы.',kind:'confirm',submit:'Удалить',danger:true});
      if(!confirmed)return;
      try{await api('/api/message/delete',{id:m.id});poll();}catch(e){notice(e.message);}
    };
    actions.append(remove);content.append(actions);
  }
  item.append(LANUI.avatar({id:m.sender,name:m.sender_name,avatar_file:m.sender_avatar},'avatar-message'),content);
  return item;
}
function renderMessages(){const files=[...history.values()].filter(m=>m.file_id&&!m.deleted).sort((a,b)=>b.id-a.id).slice(0,12);$('details-files').replaceChildren();$('details-summary').textContent=roomId!==null?'Комната для участников сети':recipient!==null?'Личный разговор':'Сообщения для всех участников сети';for(const f of files){const a=document.createElement('a');a.href='/download/'+encodeURIComponent(f.file_id);a.className='detail-file';a.textContent='↓ '+f.file_name;$('details-files').append(a);}if(!files.length)$('details-files').textContent='Пока нет файлов';const list=$('messages');const nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<100;list.replaceChildren();for(const m of [...history.values()].sort((a,b)=>a.id-b.id))list.append(messageNode(m));if(!list.children.length){const e=document.createElement('p');e.className='empty';e.textContent='Начните разговор. Напишите сообщение или поделитесь файлом.';list.append(e);}if(nearBottom)list.scrollTop=list.scrollHeight;}
async function poll(){if(polling)return;polling=true;const gen=generation;try{const data=await api(withRoute('/api/state'));if(gen!==generation)return;me=data.me;$('auth').hidden=true;$('app').hidden=false;renderSidebar(data);updateComposer(data.now);if(!history.size)more=data.has_more;for(const m of data.messages)history.set(m.id,m);$('older').hidden=!more;renderMessages();$('connection').textContent='● Сервер доступен';$('connection').classList.remove('offline');}catch(e){if(e.status===401){me=null;$('auth').hidden=false;$('app').hidden=true;}else{$('connection').textContent='Нет связи · повторяем';$('connection').classList.add('offline');}}finally{polling=false;if(gen!==generation)poll();}}
$('older').onclick=async()=>{const ids=[...history.keys()];if(!ids.length)return;const gen=generation;try{const data=await api(withRoute('/api/state?before='+Math.min(...ids)));if(gen!==generation)return;const list=$('messages'),oldHeight=list.scrollHeight;for(const m of data.messages)history.set(m.id,m);more=data.has_more;$('older').hidden=!more;renderMessages();list.scrollTop+=list.scrollHeight-oldHeight;if(!data.messages.length)more=false;}catch(e){notice(e.message);}};
$('compose').onsubmit=async e=>{e.preventDefault();const text=$('text').value.trim();if(!text)return;$('send').disabled=true;try{const r=await api('/api/message',{text,recipient,room:roomId});$('text').value='';notice(r.censored?'Слово из списка цензуры скрыто звёздочками.':'');await poll();}catch(err){notice(err.message);}finally{$('send').disabled=me?.blocked_until>Date.now()/1000;}};
$('text').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('send').disabled)$('compose').requestSubmit();}};
$('logout').onclick=async()=>{try{await api('/api/logout',{});xhr?.abort();me=null;recipient=null;roomId=null;generation++;history=new Map();$('messages').replaceChildren();$('room').textContent='Общий чат';document.body.classList.remove('chat-open','details-open');$('auth').hidden=false;$('app').hidden=true;}catch(e){notice(e.message);}};
$('profile-avatar').onclick=()=>$('avatar-file').click();
$('avatar-file').onchange=async()=>{
  const file=$('avatar-file').files[0];$('avatar-file').value='';
  if(!file)return;
  if(file.size===0||file.size>2*1024*1024){notice('Выберите изображение до 2 МБ.');return;}
  if(!['image/png','image/jpeg','image/webp'].includes(file.type)){notice('Поддерживаются PNG, JPEG и WebP.');return;}
  try{
    const response=await fetch('/api/avatar',{method:'POST',headers:{'X-LANLink':'1','Content-Type':'application/octet-stream'},body:file});
    const result=await response.json();
    if(!response.ok)throw Error(result.error||'Не удалось загрузить фото.');
    notice('Фото профиля обновлено.');poll();
  }catch(e){notice(e.message);}
};
$('remove-avatar').onclick=async()=>{
  const confirmed=await LANUI.ask({title:'Удалить фото профиля?',kind:'confirm',submit:'Удалить',danger:true});
  if(!confirmed)return;
  try{await api('/api/avatar/delete',{});notice('Фото профиля удалено.');poll();}catch(e){notice(e.message);}
};
$('attach').onclick=()=>$('file').click();$('file').onchange=()=>{const f=$('file').files[0];if(f)upload(f);$('file').value='';};
function upload(file){if(xhr){notice('Дождитесь завершения текущей передачи.');return;}if(file.size===0||file.size>50*1024*1024){notice('Выберите непустой файл размером до 50 МБ.');return;}const targetName=$('room').textContent;const req=new XMLHttpRequest();xhr=req;$('upload-panel').hidden=false;$('upload-text').textContent=file.name+' → '+targetName;$('progress').value=0;notice('');req.open('POST',withRoute('/api/upload'));req.setRequestHeader('X-LANLink','1');req.setRequestHeader('X-Filename',encodeURIComponent(file.name));req.setRequestHeader('Content-Type','application/octet-stream');req.upload.onprogress=e=>{if(e.lengthComputable)$('progress').value=e.loaded/e.total*100;};req.onload=()=>{try{const r=JSON.parse(req.responseText);if(req.status!==201)notice(r.error||'Ошибка передачи');else{notice(r.censored?'Название файла скрыто цензурой.':'Файл передан в «'+targetName+'».');poll();}}catch{notice('Сервер вернул некорректный ответ.');}};req.onerror=()=>notice('Передача прервана. Проверьте соединение.');req.onabort=()=>notice('Передача отменена.');req.onloadend=()=>{xhr=null;$('upload-panel').hidden=true;};req.send(file);}
$('cancel-upload').onclick=()=>xhr?.abort();const main=document.querySelector('main');main.ondragover=e=>{e.preventDefault();main.classList.add('drag');};main.ondragleave=e=>{if(!main.contains(e.relatedTarget))main.classList.remove('drag');};main.ondrop=e=>{e.preventDefault();main.classList.remove('drag');const files=e.dataTransfer.files;if(files.length>1)notice('Переносите по одному файлу.');else if(files[0])upload(files[0]);};
poll();setInterval(()=>{if(me)poll();},2000);
