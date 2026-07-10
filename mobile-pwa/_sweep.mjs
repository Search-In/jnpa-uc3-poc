import { chromium } from '@playwright/test';
const B='http://127.0.0.1:3002/pwa/?device=TRK-000001';
const truck={device_id:'TRK-000001',decision_path:'PRIMARY',gate_boom_delay_s:0,elevated_scrutiny:false,record:{device_id:'TRK-000001',plate:'MH04KN3106',gate_id:'GATE-3',state:'EN_ROUTE',position:{lat:18.951,lon:72.949},speed_kmh:34,heading:120,remaining_km:6.2,eta_s:1080,accuracy_m:8}};
const browser=await chromium.launch();
async function mock(ctx){
  await ctx.route('**/api/auth/**',r=>r.fulfill({json:{access_token:'x.'+btoa(JSON.stringify({exp:Math.floor(Date.now()/1000)+9999,role:'DRIVER'}))+'.y'}}));
  await ctx.route('**/api/trucks/TRK-000001',r=>r.fulfill({json:truck}));
  await ctx.route('**/api/trucks?**',r=>r.fulfill({json:{devices:[{device_id:'TRK-000001',plate:'MH04KN3106'}]}}));
  await ctx.route('**/api/parking/availability',r=>r.fulfill({json:{facilities:[{facility_id:'P1',name:'JNPA Main Parking',lat:18.957,lon:72.947,capacity:200,available:120,status:'OPEN'},{facility_id:'P2',name:'NSICT Truck Parking',lat:18.945,lon:72.955,capacity:300,available:8,status:'OPEN'}]}}));
  await ctx.route('**/api/parking/summary',r=>r.fulfill({json:{total_available:120,total_capacity:500}}));
  await ctx.route('**/api/alerts**',r=>r.fulfill({json:{alerts:[{id:'a1',kind:'CONGESTION_HIGH',severity:'critical',ts:new Date().toISOString(),payload:{gate_id:'G-2'}},{id:'a2',kind:'NO_PARKING_VIOLATION',severity:'warning',ts:new Date().toISOString(),payload:{zone_id:'Zone B'}}]}}));
  await ctx.route('**/api/vahan/rc/**',r=>r.fulfill({json:{plate:'MH04KN3106',decision_path:'LIVE_PRIMARY',record:{owner_name_masked:'R***** K****',vehicle_class:'HGV/Truck',fuel_type:'Diesel',blacklist_status:'CLEAR',insurance_valid_to:'2026-11-30',fitness_valid_to:'2027-03-15'}}}));
  await ctx.route('**/api/auth/otp/session/**',r=>r.fulfill({json:{bound:true,driver_id:'D-1'}}));
  await ctx.route('**/api/vahan/driver-intel/**',r=>r.fulfill({json:{driver:{name:'Ramesh Kumar'},dl_history:[{status:'VALID'}],vehicle_no:'MH04KN3106',violations:[]}}));
  await ctx.route('**/api/identity/enrol-request/**',r=>r.fulfill({json:{driver_id:'D-1',status:'ACTIVE',name:'Ramesh Kumar'}}));
  await ctx.route('**/api/corridor',r=>r.fulfill({json:{name:'NH-348',polyline:[[72.949,18.951],[72.948,18.966]],length_km:8,segment_count:4}}));
  await ctx.route('**/api/gates',r=>r.fulfill({json:{gates:[{id:'GATE-3',name:'Gate 3',lat:18.966,lon:72.948}]}}));
  await ctx.route('**/api/**',r=>r.fulfill({json:{}}));
}
const viewports=[[320,568],[360,640],[390,844],[412,915],[430,932],[768,1024]];
const screens=[['home','#/home'],['nav','#/map'],['trip','#/trip'],['alerts','#/alerts'],['parking','#/parking'],['vehicle','#/profile']];
const report={};
for(const [w,h] of viewports){
  const ctx=await browser.newContext({viewport:{width:w,height:h},deviceScaleFactor:2});
  await mock(ctx);
  const page=await ctx.newPage();
  const row={};
  for(const [name,hash] of screens){
    await page.goto(B+hash,{waitUntil:'domcontentloaded',timeout:20000});
    await page.waitForTimeout(name==='nav'?2500:1200);
    const m=await page.evaluate(()=>{
      const de=document.documentElement;
      const hScroll=de.scrollWidth-de.clientWidth;
      // any element wider than viewport?
      let overflow=[];
      document.querySelectorAll('*').forEach(el=>{const r=el.getBoundingClientRect(); if(r.right>window.innerWidth+1 && r.width>0 && r.width<window.innerWidth*3){const c=(el.className&&el.className.baseVal!==undefined)?el.className.baseVal:el.className; if(typeof c==='string'&&c) overflow.push(c.split(' ')[0]+':'+Math.round(r.right));}});
      // content hidden behind tabbar?
      const tb=document.querySelector('.tabbar'); const content=document.querySelector('.content');
      return {hScroll, overflow:[...new Set(overflow)].slice(0,6), tabH: tb?Math.round(tb.getBoundingClientRect().height):0};
    });
    row[name]=m;
  }
  report[w+'x'+h]=row;
  await ctx.close();
}
console.log(JSON.stringify(report,null,1));
await browser.close();
