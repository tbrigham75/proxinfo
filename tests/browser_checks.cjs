/* Developer-only browser regression checks; no PVE/PBS connections. */
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const python = process.env.PYTHON || (process.platform === 'win32' ? 'py' : 'python3');
const source = `import sys,json\nsys.path.insert(0,'tests')\nfrom test_inventory import collect, inv\nr=collect()\nr['vms'][0]['node']='pve01'\nr['vms'][1]['node']='pve02'\nfrom test_extensions import collect_pbs\nr['pbs']=collect_pbs()\nbase=r['pbs']['groups'][0]\nr['pbs']['groups']=[dict(base, backup_id=str(2000+i)) for i in range(550)]\ninv.sort_report(r,'configured')\nprint(json.dumps({'html':inv.html_report(r),'print':inv.management_summary_html(r)}))`;
const result=spawnSync(python,['-B','-c',source],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}});
assert.equal(result.status,0,result.stderr);const content=JSON.parse(result.stdout);
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_PATH?{executablePath:process.env.BROWSER_PATH}:{})});
 try{
  const page=await browser.newPage(); const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setContent(content.html);await page.evaluate(()=>showPage('review'));
  await page.locator('[data-review-vmid="101"]').first().click();
  assert.equal(await page.locator('#vms tbody tr:visible').count(),2);
  assert.equal(await page.locator('.selected-vm').count(),1);
  for(const node of ['pve01','pve02']){await page.selectOption('#node-filter',node);assert.equal(await page.locator('#vms tbody tr:visible').count(),1);}
  await page.fill('#search','nothing');assert.equal(await page.locator('#vms tbody tr:visible').count(),0);
  await page.click('#clear-filters');assert.equal(await page.locator('#vms tbody tr:visible').count(),2);
  assert.equal(await page.locator('#table-assigned-total').textContent(),'150.00 GiB');
  assert.equal(await page.locator('#table-used-total').textContent(),'20.00 GiB · Complete');
  assert.equal(await page.locator('[data-page-link="changes"]').count(),0);
  assert.deepEqual(await page.locator('.nav a').allTextContents(),['Summary','Needs Attention','PVE Hosts',"VM's",'Storage','PVE Backup Jobs','PBS Backup Storage','Management Summary','Downloads','Help','IT Reference']);
  await page.evaluate(()=>{const row=document.querySelector('#vms tbody tr');const disks=JSON.parse(row.dataset.disks);disks.push({identity:['missing','disk'],capacity:'1024',used:null,method:null});row.dataset.disks=JSON.stringify(disks);filterRows();});
  assert.equal(await page.locator('#table-used-total').textContent(),'20.00 GiB · Partial');
  assert.equal(await page.locator('#table-ram-total').textContent(),'6.00 GiB');
  await page.selectOption('#global-node-filter','pve01');assert.equal(await page.locator('#table-ram-total').textContent(),'4.00 GiB');assert.equal(await page.locator('#vms tbody tr:visible').count(),1);
  await page.selectOption('#global-node-filter','');await page.locator('#vms tbody tr').first().locator('td').nth(1).click();await page.waitForFunction(()=>document.querySelector('.vm-drawer').open);await page.locator('.vm-drawer button').first().click();
  assert.equal(await page.locator('.vm-drawer').evaluate(el=>el.open),false);
  assert.equal(await page.locator('#vms thead th').count(),11);
  await page.locator('#vms tbody tr').first().focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.vm-drawer').open);
  await page.keyboard.press('Escape');assert.equal(await page.locator('.vm-drawer').evaluate(el=>el.open),false);
  await page.locator('#vms tbody tr').first().locator('td').nth(3).click();
  await page.waitForFunction(()=>document.querySelector('.vm-drawer').open);
  await page.mouse.click(2,2);assert.equal(await page.locator('.vm-drawer').evaluate(el=>el.open),false);

  await page.locator('button[data-col="2"]').click();await page.locator('button[data-col="2"]').click();
  assert.equal(await page.locator('#vms tbody tr').first().getAttribute('data-node'),'pve02');
  await page.evaluate(()=>showPage('pbs'));await page.getByRole('button',{name:'Backups',exact:true}).click();
  assert.equal(await page.locator('#pbs-inventory tbody tr:visible').count(),50);
  await page.click('#pbs-next');assert.ok((await page.locator('#pbs-count').textContent()).includes('page 2'));
  await page.fill('#pbs-search','2549');assert.equal(await page.locator('#pbs-inventory tbody tr:visible').count(),1);
  await page.fill('#pbs-search','');await page.locator('[data-pbs-col="3"]').click();await page.locator('[data-pbs-col="3"]').click();
  assert.ok((await page.locator('#pbs-inventory tbody tr').first().textContent()).includes('2549'));
  await page.click('#theme-toggle');await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.setContent(content.print);await page.emulateMedia({media:'print'});
  for(const format of ['Letter','A4'])assert.ok((await page.pdf({format})).length>1000);
  assert.deepEqual(errors,[]);console.log('Browser checks passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
