async function fetchCustomers(){
  const r=await fetch(API+'/api/v1/variables/customers').then(x=>x.json()).catch(()=>[]);
  return r.map(x=>x.__value||x);
}
async function fetchEnvironments(customer){
  const u=API+'/api/v1/variables/environments?customer='+encodeURIComponent(customer||'');
  const r=await fetch(u).then(x=>x.json()).catch(()=>[]);
  return r.map(x=>x.__value||x);
}
// Legacy aliases
const fetchClients = fetchCustomers;
const fetchAccounts = fetchEnvironments;
function fillCombo(selId,newId,values,current){
  const sel=document.getElementById(selId),inp=document.getElementById(newId);
  sel.innerHTML='';
  (values||[]).forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;sel.appendChild(o);});
  const o=document.createElement('option');o.value='__new__';o.textContent='+ Add new…';sel.appendChild(o);
  if(current&&(values||[]).includes(current)){sel.value=current;inp.style.display='none';inp.value='';}
  else if(current){sel.value='__new__';inp.style.display='block';inp.value=current;}
  else{sel.value=(values&&values[0])||'__new__';inp.style.display=sel.value==='__new__'?'block':'none';}
}
function onCombo(selId,newId,after){
  const sel=document.getElementById(selId),inp=document.getElementById(newId);
  if(sel.value==='__new__'){inp.style.display='block';inp.focus();}
  else{inp.style.display='none';inp.value='';if(after)after();}
}
function comboVal(selId,newId){
  const sel=document.getElementById(selId);
  if(sel.value==='__new__')return document.getElementById(newId).value.trim();
  return sel.value;
}
