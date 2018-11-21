#!/usr/bin/env python3

# Following test verifies CS paging works when MS is GPRS  attached.
# See OS#2204 for more information.

from osmo_gsm_tester.testenv import *

hlr = suite.hlr()
bts = suite.bts()
pcu = bts.pcu()
mgw_msc = suite.mgw()
mgw_bsc = suite.mgw()
stp = suite.stp()
ggsn = suite.ggsn()
sgsn = suite.sgsn(hlr, ggsn)
msc = suite.msc(hlr, mgw_msc, stp)
bsc = suite.bsc(msc, mgw_bsc, stp)
ms_mo = suite.modem()
ms_mt = suite.modem()

bsc.bts_add(bts)
sgsn.bts_add(bts)

print('start network...')
hlr.start()
stp.start()
ggsn.start()
sgsn.start()
msc.start()
mgw_msc.start()
mgw_bsc.start()
bsc.start()

bts.start()
wait(bsc.bts_is_connected, bts)
print('Waiting for bts to be ready...')
wait(bts.ready_for_pcu)
pcu.start()

hlr.subscriber_add(ms_mo)
hlr.subscriber_add(ms_mt)

ms_mo.connect(msc.mcc_mnc())
ms_mt.connect(msc.mcc_mnc())
ms_mo.attach()
ms_mt.attach()

ms_mo.log_info()
ms_mt.log_info()

print('waiting for modems to attach...')
wait(ms_mo.is_connected, msc.mcc_mnc())
wait(ms_mt.is_connected, msc.mcc_mnc())
wait(msc.subscriber_attached, ms_mo, ms_mt)

print('waiting for modems to attach to data services...')
wait(ms_mo.is_attached)
wait(ms_mt.is_attached)

# We need to use inet46 since ofono qmi only uses ipv4v6 eua (OS#2713)
ctx_id_v4_mo = ms_mo.activate_context(apn='inet46', protocol=ms_mo.CTX_PROT_IPv4)
ctx_id_v4_mt = ms_mt.activate_context(apn='inet46', protocol=ms_mt.CTX_PROT_IPv4)

assert len(ms_mo.call_id_list()) == 0 and len(ms_mt.call_id_list()) == 0
mo_cid = ms_mo.call_dial(ms_mt)
mt_cid = ms_mt.call_wait_incoming(ms_mo)
print('dial success')

assert not ms_mo.call_is_active(mo_cid) and not ms_mt.call_is_active(mt_cid)
ms_mt.call_answer(mt_cid)
wait(ms_mo.call_is_active, mo_cid)
wait(ms_mt.call_is_active, mt_cid)
print('answer success, call established and ongoing')

sleep(5) # maintain the call active for 5 seconds

assert ms_mo.call_is_active(mo_cid) and ms_mt.call_is_active(mt_cid)
ms_mt.call_hangup(mt_cid)
wait(lambda: len(ms_mo.call_id_list()) == 0 and len(ms_mt.call_id_list()) == 0)
print('hangup success')

ms_mo.deactivate_context(ctx_id_v4_mo)
ms_mt.deactivate_context(ctx_id_v4_mt)
