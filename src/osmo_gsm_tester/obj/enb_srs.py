# osmo_gsm_tester: specifics for running an SRS eNodeB process
#
# Copyright (C) 2020 by sysmocom - s.f.m.c. GmbH
#
# Author: Pau Espin Pedrol <pespin@sysmocom.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import pprint

from ..core import log, util, config, template, process, remote
from ..core.event_loop import MainLoop
from . import enb
from . import rfemu
from .srslte_common import srslte_common

from ..core import schema

def on_register_schemas():
    resource_schema = {
        'prerun_scripts[]': schema.STR,
        'postrun_scripts[]': schema.STR,
        'remote_dir': schema.STR
        }
    schema.register_resource_schema('enb', resource_schema)

    config_schema = {
        'enable_malloc_interceptor': schema.BOOL_STR,
        'enable_pcap': schema.BOOL_STR,
        'enable_tracing': schema.BOOL_STR,
        'enable_ul_qam64': schema.BOOL_STR,
        'log_all_level': schema.STR,
        }
    schema.register_config_schema('enb', config_schema)

def rf_type_valid(rf_type_str):
    return rf_type_str in ('zmq', 'uhd', 'soapy', 'bladerf', 'fapi')

class srsENB(enb.eNodeB, srslte_common):

    REMOTE_DIR = '/osmo-gsm-tester-srsenb'
    BINFILE = 'srsenb'
    CFGFILE = 'srsenb.conf'
    CFGFILE_SIB = 'srsenb_sib.conf'
    CFGFILE_RR = 'srsenb_rr.conf'
    CFGFILE_RB = 'srsenb_rb.conf'
    LOGFILE = 'srsenb.log'
    PCAPFILE = 'srsenb_mac.pcap'
    S1AP_PCAPFILE = 'srsenb_s1ap.pcap'
    TRACINGFILE = 'srsenb_tracing.log'
    METRICSFILE = 'srsenb_metrics.csv'
    INTERCEPTORFILE = 'srsenb_minterceptor.log'

    def __init__(self, testenv, conf):
        super().__init__(testenv, conf, srsENB.BINFILE)
        srslte_common.__init__(self)
        self.ue = None
        self.run_dir = None
        self.gen_conf = None
        self.config_file = None
        self.config_sib_file = None
        self.config_rr_file = None
        self.config_rb_file = None
        self.tracing_file = None
        self.interceptor_file = None
        self.log_file = None
        self.pcap_file = None
        self.s1ap_pcap_file = None
        self.process = None
        self.rem_host = None
        self.remote_run_dir = None
        self.remote_config_file =  None
        self.remote_config_sib_file = None
        self.remote_config_rr_file = None
        self.remote_config_rb_file = None
        self.remote_log_file = None
        self.remote_pcap_file = None
        self.remote_s1ap_pcap_file = None
        self.remote_tracing_file = None
        self.remote_metrics_file = None
        self.remote_interceptor_file = None
        self.enable_pcap = False
        self.enable_ul_qam64 = False
        self.enable_tracing = False
        self.enable_malloc_interceptor = False
        self.metrics_file = None
        self.have_metrics_file = False
        self.stop_sleep_time = 6 # We require at most 5s to stop
        self.testenv = testenv
        self._additional_args = []
        if not rf_type_valid(conf.get('rf_dev_type', None)):
            raise log.Error('Invalid rf_dev_type=%s' % conf.get('rf_dev_type', None))

    def cleanup(self):
        if self.process is None:
            return
        if self._run_node.is_local():
            return

        # Make sure we give the UE time to tear down
        self.sleep_after_stop()

        # Execute the post run tasks.
        if not self.postrun_tasks():
            self.log('Could not execute the post run tasks')

        # copy back files (may not exist, for instance if there was an early error of process):
        self.scp_back_metrics(raiseException=False)

        # copy back files (may not exist, for instance if there was an early error of process):
        try:
            self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
        except Exception as e:
            self.log(repr(e))
        if self.enable_pcap:
            try:
                self.rem_host.scpfrom('scp-back-pcap', self.remote_pcap_file, self.pcap_file)
            except Exception as e:
                self.log(repr(e))
            try:
                self.rem_host.scpfrom('scp-back-s1-pcap', self.remote_s1ap_pcap_file, self.s1ap_pcap_file)
            except Exception as e:
                self.log(repr(e))
        if self.enable_tracing:
            try:
                self.rem_host.scpfrom('scp-back-tracing', self.remote_tracing_file, self.tracing_file)
            except Exception as e:
                self.log(repr(e))

        if self.enable_malloc_interceptor:
            try:
                self.rem_host.scpfrom('scp-back-interceptor', self.remote_interceptor_file, self.interceptor_file)
            except Exception as e:
                self.log(repr(e))

        # Collect KPIs for each TC
        self.testenv.test().set_kpis(self.get_kpi_tree())
        # Clean up for parent class:
        super().cleanup()

    def scp_back_metrics(self, raiseException=True):
        ''' Copy back metrics only if they have not been copied back yet '''
        if not self.have_metrics_file:
            # file is not properly flushed until the process has stopped.
            if self.running():
                self.stop()

            # only SCP back if not running locally
            if not self._run_node.is_local():
                try:
                    self.rem_host.scpfrom('scp-back-metrics', self.remote_metrics_file, self.metrics_file)
                except Exception as e:
                    if raiseException:
                        self.err('Failed copying back metrics file from remote host')
                        raise e
                    else:
                        # only log error
                        self.log(repr(e))
            # make sure to only call it once
            self.have_metrics_file = True
        else:
            self.dbg('Metrics have already been copied back')

    def run_task(self, task):
        # Get the arguments.
        args_index = task.find('args=')

        args = ()
        # No arguments, all the string is the script.
        if args_index == -1:
            index = task.rfind('/')
            task_name = task [index + 1:]
            run_dir = util.Dir(self.run_dir.new_dir(task_name))
            args = (task,)
            self.log(f'task name is: {task_name}')
            self.log(f'Running the script: {task} in the run dir: {run_dir}')
        else:
            ntask = task[:args_index - 1]
            index = ntask.rfind('/')
            task_name = ntask [index + 1:]
            run_dir = util.Dir(self.run_dir.new_dir(task_name))
            args = (ntask,)
            args += tuple(task[args_index + 5:].split(','))
            self.log(f'task name is: {task_name}')
            self.log(f'Running the script: {task} in the run dir: {run_dir} with args: {args}')

        proc = process.Process(task_name, run_dir, args)
        # Set the timeout to a high value 20 minutes.
        proc.set_default_wait_timeout(1200)
        returncode = proc.launch_sync()
        if returncode != 0:
            raise log.Error('Error executing the pre run scripts. Aborting')
            return False

        return True

    # Runs all the tasks that are intended to run before the execution of the eNodeb.
    def prerun_tasks(self):
        prerun_tasklist = self._conf.get('prerun_scripts', None)
        if not prerun_tasklist:
            return True

        for task in prerun_tasklist:
            if not self.run_task(task):
                return False

        return True

    # Runs all the tasks that are intended to run after the execution of the eNodeb.
    def postrun_tasks(self):
        postrun_tasklist = self._conf.get('postrun_scripts', None)
        if not postrun_tasklist:
            return True

        for task in postrun_tasklist:
            if not self.run_task(task):
                return False

        return True

    def start(self, epc):
        self.log('Starting srsENB')
        self._epc = epc
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.configure()

        if not self.prerun_tasks():
            self.log('Pre run tasks failed. Aborting')
            return

        if self._run_node.is_local():
            self.start_locally()
        else:
            self.start_remotely()

        # send t+Enter to enable console trace
        self.dbg('Enabling console trace')
        self.process.stdin_write('t\n')

    def stop(self):
        # Implemented in srslte_common.py
        srslte_common.stop(self)

    def start_remotely(self):
        remote_env = { 'LD_LIBRARY_PATH': self.remote_inst.child('lib') }
        # Add the malloc interceptor env variable when it's required.
        if self.enable_malloc_interceptor:
            path = self._run_node.lib_path_malloc_interceptor()
            if not path:
                raise log.Error('Could not get the environment variables. Aborting')

            self.log(f'Setting LD_PRELOAD var to value: {path}')
            remote_env['LD_PRELOAD'] = path

        remote_binary = self.remote_inst.child('bin', srsENB.BINFILE)
        args = (remote_binary, self.remote_config_file)
        args += tuple(self._additional_args)

        # Force the output of the malloc interceptor to the interceptor_file.
        if self.enable_malloc_interceptor:
            args += tuple([f" 2> {self.remote_interceptor_file}"])

        self.process = self.rem_host.RemoteProcessSafeExit(srsENB.BINFILE, self.remote_run_dir, args, remote_env=remote_env, wait_time_sec=7)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        binary = self.inst.child('bin', srsENB.BINFILE)
        lib = self.inst.child('lib')
        env = { 'LD_LIBRARY_PATH': util.prepend_library_path(lib) }
        args = (binary, os.path.abspath(self.config_file))
        args += tuple(self._additional_args)

        self.process = process.Process(self.name(), self.run_dir, args, env=env)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def gen_conf_file(self, path, filename, values):
        self.dbg('srsENB ' + filename + ':\n' + pprint.pformat(values))

        with open(path, 'w') as f:
            r = template.render(filename, values)
            self.dbg(r)
            f.write(r)

    def configure(self):
        self.inst = util.Dir(os.path.abspath(self.testenv.suite().trial().get_inst('srsran',  self._run_node.run_label())))
        if not os.path.isdir(self.inst.child('lib')):
            raise log.Error('No lib/ in', self.inst)
        if not self.inst.isfile('bin', srsENB.BINFILE):
            raise log.Error('No %s binary in' % srsENB.BINFILE, self.inst)

        self.config_file = self.run_dir.child(srsENB.CFGFILE)
        self.config_sib_file = self.run_dir.child(srsENB.CFGFILE_SIB)
        self.config_rr_file = self.run_dir.child(srsENB.CFGFILE_RR)
        self.config_rb_file = self.run_dir.child(srsENB.CFGFILE_RB)
        self.log_file = self.run_dir.child(srsENB.LOGFILE)
        self.pcap_file = self.run_dir.child(srsENB.PCAPFILE)
        self.s1ap_pcap_file = self.run_dir.child(srsENB.S1AP_PCAPFILE)
        self.metrics_file = self.run_dir.child(srsENB.METRICSFILE)
        self.tracing_file = self.run_dir.child(srsENB.TRACINGFILE)
        self.interceptor_file = self.run_dir.child(srsENB.INTERCEPTORFILE)

        if not self._run_node.is_local():
            self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr())
            remote_prefix_dir = util.Dir(srsENB.REMOTE_DIR)

            # Modify the default remote directory if it is provided by the configuration.
            remote_path = self._conf.get('remote_dir', None)
            if remote_path:
                remote_prefix_dir = util.Dir(remote_path)
                self.log(f'Setting the remote dir to: {remote_path}')

            self.remote_inst = util.Dir(remote_prefix_dir.child(os.path.basename(str(self.inst))))
            self.remote_run_dir = util.Dir(remote_prefix_dir.child(self.name()))

            self.remote_config_file = self.remote_run_dir.child(srsENB.CFGFILE)
            self.remote_config_sib_file = self.remote_run_dir.child(srsENB.CFGFILE_SIB)
            self.remote_config_rr_file = self.remote_run_dir.child(srsENB.CFGFILE_RR)
            self.remote_config_rb_file = self.remote_run_dir.child(srsENB.CFGFILE_RB)
            self.remote_log_file = self.remote_run_dir.child(srsENB.LOGFILE)
            self.remote_pcap_file = self.remote_run_dir.child(srsENB.PCAPFILE)
            self.remote_s1ap_pcap_file = self.remote_run_dir.child(srsENB.S1AP_PCAPFILE)
            self.remote_metrics_file = self.remote_run_dir.child(srsENB.METRICSFILE)
            self.remote_tracing_file = self.remote_run_dir.child(srsENB.TRACINGFILE)
            self.remote_interceptor_file = self.remote_run_dir.child(srsENB.INTERCEPTORFILE)

        values = super().configure(['srsenb'])

        metricsfile = self.metrics_file if self._run_node.is_local() else self.remote_metrics_file
        tracingfile = self.tracing_file if self._run_node.is_local() else self.remote_tracing_file
        sibfile = self.config_sib_file if self._run_node.is_local() else self.remote_config_sib_file
        rrfile = self.config_rr_file if self._run_node.is_local() else self.remote_config_rr_file
        rbfile = self.config_rb_file if self._run_node.is_local() else self.remote_config_rb_file
        logfile = self.log_file if self._run_node.is_local() else self.remote_log_file
        pcapfile = self.pcap_file if self._run_node.is_local() else self.remote_pcap_file
        s1ap_pcapfile = self.s1ap_pcap_file if self._run_node.is_local() else self.remote_s1ap_pcap_file
        config.overlay(values, dict(enb=dict(metrics_filename=metricsfile,
                                             tracing_filename=tracingfile,
                                             sib_filename=sibfile,
                                             rr_filename=rrfile,
                                             rb_filename=rbfile,
                                             log_filename=logfile,
                                             pcap_filename=pcapfile,
                                             s1ap_pcap_filename=s1ap_pcapfile,
                                             )))

        # Retrieve the malloc interceptor option.
        self.enable_malloc_interceptor = util.str2bool(values['enb'].get('enable_malloc_interceptor', 'false'))

        # Convert parsed boolean string to Python boolean:
        self.enable_pcap = util.str2bool(values['enb'].get('enable_pcap', 'false'))
        config.overlay(values, dict(enb={'enable_pcap': self.enable_pcap}))

        self.enable_tracing = util.str2bool(values['enb'].get('enable_tracing', 'false'))
        config.overlay(values, dict(enb={'enable_tracing': self.enable_tracing}))

        self.enable_ul_qam64 = util.str2bool(values['enb'].get('enable_ul_qam64', 'false'))
        config.overlay(values, dict(enb={'enable_ul_qam64': self.enable_ul_qam64}))

        config.overlay(values, dict(enb={'enable_dl_awgn': util.str2bool(values['enb'].get('enable_dl_awgn', 'false'))}))
        config.overlay(values, dict(enb={'rf_dev_sync': values['enb'].get('rf_dev_sync', None)}))

        self._additional_args = []
        for add_args in values['enb'].get('additional_args', []):
            self._additional_args += add_args.split()

        # We need to set some specific variables programatically here to match IP addresses:
        if self._conf.get('rf_dev_type') == 'zmq':
            rf_dev_args = self.get_zmq_rf_dev_args(values)
            config.overlay(values, dict(enb=dict(rf_dev_args=rf_dev_args)))

        # Set UHD frame size as a function of the cell bandwidth on B2XX
        if self._conf.get('rf_dev_type') == 'uhd' and values['enb'].get('rf_dev_args', None) is not None:
            if 'b200' in values['enb'].get('rf_dev_args'):
                rf_dev_args = values['enb'].get('rf_dev_args', '')
                rf_dev_args += ',' if rf_dev_args != '' and not rf_dev_args.endswith(',') else ''

                if self._num_prb == 75:
                    rf_dev_args += 'master_clock_rate=15.36e6,'

                if self._txmode <= 2:
                    # SISO config
                    if self._num_prb < 25:
                        rf_dev_args += 'send_frame_size=512,recv_frame_size=512'
                    elif self._num_prb == 25:
                        rf_dev_args += 'send_frame_size=1024,recv_frame_size=1024'
                    else:
                        rf_dev_args += ''
                else:
                    # MIMO config
                    rf_dev_args += 'num_recv_frames=64,num_send_frames=64'
                    if self._num_prb > 50:
                        # Reduce over the wire format to sc12
                        rf_dev_args += ',otw_format=sc12'

                config.overlay(values, dict(enb=dict(rf_dev_args=rf_dev_args)))

        if self._conf.get('rf_dev_type') == 'fapi':
            rf_dev_args = ''
            config.overlay(values, dict(enb=dict(rf_dev_args=rf_dev_args)))

        self.gen_conf = values

        self.gen_conf_file(self.config_file, srsENB.CFGFILE, values)
        self.gen_conf_file(self.config_sib_file, srsENB.CFGFILE_SIB, values)
        self.gen_conf_file(self.config_rr_file, srsENB.CFGFILE_RR, values)
        self.gen_conf_file(self.config_rb_file, srsENB.CFGFILE_RB, values)

        if not self._run_node.is_local():
            self.rem_host.recreate_remote_dir(self.remote_inst)
            self.rem_host.scp('scp-inst-to-remote', str(self.inst), remote_prefix_dir)
            self.rem_host.recreate_remote_dir(self.remote_run_dir)
            self.rem_host.scp('scp-cfg-to-remote', self.config_file, self.remote_config_file)
            self.rem_host.scp('scp-cfg-sib-to-remote', self.config_sib_file, self.remote_config_sib_file)
            self.rem_host.scp('scp-cfg-rr-to-remote', self.config_rr_file, self.remote_config_rr_file)
            self.rem_host.scp('scp-cfg-rb-to-remote', self.config_rb_file, self.remote_config_rb_file)

    def ue_add(self, ue):
        if self.ue is not None:
            raise log.Error("More than one UE per ENB not yet supported (ZeroMQ)")
        self.ue = ue

    def running(self):
        return not self.process.terminated()

    def get_counter(self, counter_name):
        if counter_name == 'prach_received':
            return self.process.get_counter_stdout('RACH:')
        raise log.Error('counter %s not implemented!' % counter_name)

    def get_kpis(self):
        return srslte_common.get_kpis(self)

    def get_rfemu(self, cell=0, dl=True):
        cell_list = self.gen_conf['enb'].get('cell_list', None)
        if cell_list is None or len(cell_list) < cell + 1:
            raise log.Error('cell_list attribute or subitem not found!')
        rfemu_cfg = cell_list[cell].get('dl_rfemu', None)
        if rfemu_cfg is None:
            raise log.Error('rfemu attribute not found in cell_list item!')
        if rfemu_cfg['type'] == 'srsenb_stdin' or rfemu_cfg['type'] == 'gnuradio_zmq':
            # These fields are required so the rfemu class can interact with us:
             config.overlay(rfemu_cfg, dict(enb=self,
                                            cell_id=cell_list[cell]['cell_id']))

        rfemu_obj = rfemu.get_instance_by_type(rfemu_cfg['type'], rfemu_cfg)
        return rfemu_obj

    def ue_max_rate(self, downlink=True, num_carriers=1):
        # The max rate for a single UE per PRB configuration in TM1 with MCS 28
        if 'dl_qam256' in self.ue.features():
            max_phy_rate_tm1_dl = {6: 4.4e6,
                                   15: 14e6,
                                   25: 24e6,
                                   50: 49e6,
                                   75: 75e6,
                                   100: 98e6}
        else:
            max_phy_rate_tm1_dl = {6: 3.3e6,
                                   15: 11e6,
                                   25: 18e6,
                                   50: 36e6,
                                   75: 55e6,
                                   100: 75e6}

        if self.enable_ul_qam64 and 'ul_qam64' in self.ue.features():
            max_phy_rate_tm1_ul = { 6 : 2.7e6,
                                    15 : 6.5e6,
                                    25 : 14e6,
                                    50 : 32e6,
                                    75 : 34e6,
                                    100 : 71e6 }
        else:
            max_phy_rate_tm1_ul = { 6 : 1.7e6,
                                    15 : 4.7e6,
                                    25 : 10e6,
                                    50 : 23e6,
                                    75 : 34e6,
                                    100 : 51e6 }

        if downlink:
            max_rate = max_phy_rate_tm1_dl[self.num_prb()]
        else:
            max_rate = max_phy_rate_tm1_ul[self.num_prb()]

        # MIMO only supported for Downlink
        if downlink:
            if self._txmode > 2:
                max_rate *= 2

            # For 6 PRBs the max throughput is significantly lower
            if self._txmode >= 2 and self.num_prb() == 6:
                max_rate *= 0.85

        # Assume we schedule all carriers
        max_rate *= num_carriers

        # Reduce expected UL rate due to missing extendedBSR support (see issue #1708)
        if downlink == False and num_carriers == 4 and self.num_prb() == 100:
            # all carriers run at 70% approx.
            max_rate *= 0.7

        return max_rate

# vim: expandtab tabstop=4 shiftwidth=4
