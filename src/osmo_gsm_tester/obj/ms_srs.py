# osmo_gsm_tester: specifics for running an SRS UE process
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
import re

from ..core import log, util, config, template, process, remote
from ..core import schema
from .run_node import RunNode
from .ms import MS
from .srslte_common import srslte_common

def rf_type_valid(rf_type_str):
    return rf_type_str in ('zmq', 'uhd', 'soapy', 'bladerf')

def on_register_schemas():
    resource_schema = {
        'rf_dev_type': schema.STR,
        'rf_dev_args': schema.STR,
        'rf_dev_sync': schema.STR,
        'num_carriers': schema.UINT,
        'num_nr_carriers': schema.UINT,
        'additional_args[]': schema.STR,
        'airplane_t_on_ms': schema.INT,
        'airplane_t_off_ms': schema.INT,
        'tx_gain': schema.INT,
        'rx_gain': schema.INT,
        'freq_offset': schema.INT,
        'force_ul_amplitude': schema.STR,
        'dl_freq': schema.STR,
        'ul_freq': schema.STR,
        'prerun_scripts[]': schema.STR,
        }
    for key, val in RunNode.schema().items():
        resource_schema['run_node.%s' % key] = val
    schema.register_resource_schema('modem', resource_schema)

    config_schema = {
        'enable_pcap': schema.BOOL_STR,
        'log_all_level': schema.STR,
        'log_nas_level': schema.STR,
        'nr_short_sn_support': schema.BOOL_STR,
        'rrc_release': schema.INT,
        'ue_category': schema.INT,
        'ue_category_dl': schema.INT,
        'ue_category_ul': schema.INT,
        }
    schema.register_config_schema('modem', config_schema)

#reference: srsLTE.git srslte_symbol_sz()
def num_prb2symbol_sz(num_prb):
    if num_prb == 6:
        return 128
    if num_prb == 50:
        return 768
    if num_prb == 75:
        return 1024
    return 1536

def num_prb2base_srate(num_prb):
    return num_prb2symbol_sz(num_prb) * 15 * 1000

class srsUE(MS, srslte_common):

    REMOTE_DIR = '/osmo-gsm-tester-srsue'
    BINFILE = 'srsue'
    CFGFILE = 'srsue.conf'
    PCAPFILE = 'srsue.pcap'
    LOGFILE = 'srsue.log'
    METRICSFILE = 'srsue_metrics.csv'

    def __init__(self, testenv, conf):
        self._run_node = RunNode.from_conf(conf.get('run_node', {}))
        super().__init__('srsue_%s' % self.addr(), testenv, conf)
        srslte_common.__init__(self)
        self.enb = None
        self.run_dir = None
        self.config_file = None
        self.log_file = None
        self.pcap_file = None
        self.metrics_file = None
        self.have_metrics_file = False
        self.process = None
        self.rem_host = None
        self.remote_inst = None
        self.remote_run_dir = None
        self.remote_config_file = None
        self.remote_log_file = None
        self.remote_pcap_file = None
        self.remote_metrics_file = None
        self.enable_pcap = False
        self.num_carriers = 1
        self.num_nr_carriers = 0
        self._additional_args = []
        if not rf_type_valid(conf.get('rf_dev_type', None)):
            raise log.Error('Invalid rf_dev_type=%s' % conf.get('rf_dev_type', None))
        self._zmq_base_bind_port = None
        if conf.get('rf_dev_type') == 'zmq':
            # Define all 4 possible local RF ports (2x CA with 2x2 MIMO)
            self._zmq_base_bind_port = self.testenv.suite().resource_pool().next_zmq_port_range(self, 4)

    def cleanup(self):
        if self.process is None:
            return
        if self._run_node.is_local():
            return

        # Make sure we give the UE time to tear down
        self.sleep_after_stop()

        # copy back files (may not exist, for instance if there was an early error of process):
        self.scp_back_metrics(raiseException=False)
        try:
            self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
        except Exception as e:
            self.log(repr(e))
        if self.enable_pcap:
            try:
                self.rem_host.scpfrom('scp-back-pcap', self.remote_pcap_file, self.pcap_file)
            except Exception as e:
                self.log(repr(e))

        # Collect KPIs for each TC
        self.testenv.test().set_kpis(self.get_kpi_tree())

    def features(self):
        return self._conf.get('features', [])

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

    def netns(self):
        return "srsue1"

    def zmq_base_bind_port(self):
        return self._zmq_base_bind_port

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

    # Runs all the tasks that are intended to run before the execution of the MS.
    def prerun_tasks(self):
        prerun_tasklist = self._conf.get('prerun_scripts', None)
        if not prerun_tasklist:
            return True

        for task in prerun_tasklist:
            if not self.run_task(task):
                return False

        return True

    def connect(self, enb):
        self.log('Starting srsue')
        self.enb = enb
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.configure()

        if not self.prerun_tasks():
            return

        if self._run_node.is_local():
            self.start_locally()
        else:
            self.start_remotely()

        # send t+Enter to enable console trace
        self.dbg('Enabling console trace')
        self.process.stdin_write('t\n')

    def start_remotely(self):
        remote_lib = self.remote_inst.child('lib')
        remote_binary = self.remote_inst.child('bin', srsUE.BINFILE)
        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsue')
        # srsue binary needs patchelf >= 0.9+52 to avoid failing during patch. OS#4389, patchelf-GH#192.
        self.rem_host.change_elf_rpath(remote_binary, remote_lib)

        # srsue requires CAP_SYS_ADMIN to jump to net network namespace: netns(CLONE_NEWNET):
        # srsue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_SYS_ADMIN+CAP_NET_ADMIN capability to srsue')
        self.rem_host.setcap_netsys_admin(remote_binary)

        self.log('Creating netns %s' % self.netns())
        self.rem_host.create_netns(self.netns())

        args = (remote_binary, self.remote_config_file, '--gw.netns=' + self.netns())
        args += tuple(self._additional_args)

        self.process = self.rem_host.RemoteProcessSafeExit(srsUE.BINFILE, self.remote_run_dir, args)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        binary = self.inst.child('bin', srsUE.BINFILE)
        lib = self.inst.child('lib')
        env = {}

        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsue')
        util.change_elf_rpath(binary, util.prepend_library_path(lib), self.run_dir.new_dir('patchelf'))

        # srsue requires CAP_SYS_ADMIN to jump to net network namespace: netns(CLONE_NEWNET):
        # srsue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_SYS_ADMIN+CAP_NET_ADMIN capability to srsue')
        util.setcap_netsys_admin(binary, self.run_dir.new_dir('setcap_netsys_admin'))

        self.log('Creating netns %s' % self.netns())
        util.create_netns(self.netns(), self.run_dir.new_dir('create_netns'))

        args = (binary, os.path.abspath(self.config_file), '--gw.netns=' + self.netns())
        args += tuple(self._additional_args)

        self.process = process.Process(self.name(), self.run_dir, args, env=env)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def configure(self):
        self.inst = util.Dir(os.path.abspath(self.testenv.suite().trial().get_inst('srsran', self._run_node.run_label())))
        if not os.path.isdir(self.inst.child('lib')):
            raise log.Error('No lib/ in', self.inst)
        if not self.inst.isfile('bin', srsUE.BINFILE):
            raise log.Error('No %s binary in' % srsUE.BINFILE, self.inst)

        self.config_file = self.run_dir.child(srsUE.CFGFILE)
        self.log_file = self.run_dir.child(srsUE.LOGFILE)
        self.pcap_file = self.run_dir.child(srsUE.PCAPFILE)
        self.metrics_file = self.run_dir.child(srsUE.METRICSFILE)

        if not self._run_node.is_local():
                self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr())
                remote_prefix_dir = util.Dir(srsUE.REMOTE_DIR)
                self.remote_inst = util.Dir(remote_prefix_dir.child(os.path.basename(str(self.inst))))
                self.remote_run_dir = util.Dir(remote_prefix_dir.child(srsUE.BINFILE))
                self.remote_config_file = self.remote_run_dir.child(srsUE.CFGFILE)
                self.remote_log_file = self.remote_run_dir.child(srsUE.LOGFILE)
                self.remote_pcap_file = self.remote_run_dir.child(srsUE.PCAPFILE)
                self.remote_metrics_file = self.remote_run_dir.child(srsUE.METRICSFILE)

        values = dict(ue=config.get_defaults('srsue'))
        config.overlay(values, dict(ue=self.testenv.suite().config().get('modem', {})))
        config.overlay(values, dict(ue=self._conf))
        config.overlay(values, dict(ue=dict(num_antennas = self.enb.num_ports(),
                                            opc = self.opc())))

        metricsfile = self.metrics_file if self._run_node.is_local() else self.remote_metrics_file
        logfile = self.log_file if self._run_node.is_local() else self.remote_log_file
        pcapfile = self.pcap_file if self._run_node.is_local() else self.remote_pcap_file
        config.overlay(values, dict(ue=dict(metrics_filename=metricsfile,
                                             log_filename=logfile,
                                             pcap_filename=pcapfile)))

        # Convert parsed boolean string to Python boolean:
        self.enable_pcap = util.str2bool(values['ue'].get('enable_pcap', 'false'))
        config.overlay(values, dict(ue={'enable_pcap': self.enable_pcap}))

        self._additional_args = []
        for add_args in values['ue'].get('additional_args', []):
            self._additional_args += add_args.split()

        self.num_carriers = int(values['ue'].get('num_carriers', 1))
        self.num_nr_carriers = int(values['ue'].get('num_nr_carriers', 0))

        # Simply pass-through the sync options
        config.overlay(values, dict(ue={'rf_dev_sync': values['ue'].get('rf_dev_sync', None)}))

        # We need to set some specific variables programatically here to match IP addresses:
        if self._conf.get('rf_dev_type') == 'zmq':
            base_srate = num_prb2base_srate(self.enb.num_prb())

            # Define all 8 possible RF ports (2x CA with 2x2 MIMO)
            rf_dev_args = self.enb.get_zmq_rf_dev_args_for_ue(self)

            if self.num_carriers == 1:
                # Single carrier
                if self.enb.num_ports() == 1 and self.num_nr_carriers == 0:
                    # SISO
                    rf_dev_args += ',rx_freq0=2630e6,tx_freq0=2510e6'
                elif self.enb.num_ports() == 2:
                    # MIMO
                    rf_dev_args += ',rx_freq0=2630e6,rx_freq1=2630e6,tx_freq0=2510e6,tx_freq1=2510e6'
            elif self.num_carriers == 2:
                # 2x CA
                if self.enb.num_ports() == 1:
                    # SISO
                    rf_dev_args += ',rx_freq0=2630e6,rx_freq1=2650e6,tx_freq0=2510e6,tx_freq1=2530e6'
                elif self.enb.num_ports() == 2:
                    # MIMO
                    rf_dev_args += ',rx_freq0=2630e6,rx_freq1=2630e6,rx_freq2=2650e6,rx_freq3=2650e6,tx_freq0=2510e6,tx_freq1=2510e6,tx_freq2=2530e6,tx_freq3=2530e6'
            elif self.num_carriers == 4:
                # 4x CA
                if self.enb.num_ports() == 1:
                    # SISO
                    rf_dev_args += ',rx_freq0=2630e6,rx_freq1=2650e6,rx_freq2=2670e6,rx_freq3=2680e6,tx_freq0=2510e6,tx_freq1=2530e6,tx_freq2=2550e6,tx_freq3=2560e6'
                elif self.enb.num_ports() == 2:
                    # MIMO
                    raise log.Error("4 carriers with MIMO isn't supported")
            else:
                # flag
                raise log.Error('No rx/tx frequencies given for {} carriers' % self.num_carriers)

            rf_dev_args += ',id=ue,base_srate='+ str(base_srate)
            config.overlay(values, dict(ue=dict(rf_dev_args=rf_dev_args)))

        # Set UHD frame size as a function of the cell bandwidth on B2XX
        if self._conf.get('rf_dev_type') == 'uhd' and values['ue'].get('rf_dev_args', None) is not None:
            if 'b200' in values['ue'].get('rf_dev_args'):
                rf_dev_args = values['ue'].get('rf_dev_args', '')
                rf_dev_args += ',' if rf_dev_args != '' and not rf_dev_args.endswith(',') else ''

                if self.enb.num_prb() == 75:
                    rf_dev_args += 'master_clock_rate=15.36e6,'

                if self.enb.num_ports() == 1:
                    # SISO config
                    if self.enb.num_prb() < 25:
                        rf_dev_args += 'send_frame_size=512,recv_frame_size=512'
                    elif self.enb.num_prb() == 25:
                        rf_dev_args += 'send_frame_size=1024,recv_frame_size=1024'
                    else:
                        rf_dev_args += ''
                else:
                    # MIMO config
                    rf_dev_args += 'num_recv_frames=64,num_send_frames=64'
                    # For the UE the otw12 format doesn't seem to work very well

                config.overlay(values, dict(ue=dict(rf_dev_args=rf_dev_args)))

        self.dbg('SRSUE CONFIG:\n' + pprint.pformat(values))

        with open(self.config_file, 'w') as f:
            r = template.render(srsUE.CFGFILE, values)
            self.dbg(r)
            f.write(r)

        if not self._run_node.is_local():
            self.rem_host.recreate_remote_dir(self.remote_inst)
            self.rem_host.scp('scp-inst-to-remote', str(self.inst), remote_prefix_dir)
            self.rem_host.recreate_remote_dir(self.remote_run_dir)
            self.rem_host.scp('scp-cfg-to-remote', self.config_file, self.remote_config_file)

    def is_rrc_connected(self):
        ''' Check whether UE is RRC connected using console message '''
        pos_connected = (self.process.get_stdout() or '').rfind('RRC Connected')
        pos_released = (self.process.get_stdout() or '').rfind('RRC IDLE')
        return pos_connected > pos_released

    def is_registered(self, mcc_mnc=None):
        ''' Checks if UE is EMM registered '''
        return 'Network attach successful.' in (self.process.get_stdout() or '')

    def get_assigned_addr(self, ipv6=False):
        if ipv6:
            raise log.Error('IPv6 not implemented!')
        else:
            stdout_lines = (self.process.get_stdout() or '').splitlines()
            for line in reversed(stdout_lines):
                if line.find('Network attach successful. IP: ') != -1:
                    ipv4_addr = re.findall( r'[0-9]+(?:\.[0-9]+){3}', line)
                    return ipv4_addr[0]
            return None

    def running(self):
        return not self.process.terminated()

    def addr(self):
        return self._run_node.run_addr()

    def run_node(self):
        return self._run_node

    def run_netns_wait(self, name, popen_args):
        if self._run_node.is_local():
            proc = process.NetNSProcess(name, self.run_dir.new_dir(name), self.netns(), popen_args, env={})
        else:
            proc = self.rem_host.RemoteNetNSProcess(name, self.netns(), popen_args, env={})
        proc.launch_sync()
        return proc

    def get_counter(self, counter_name):
        if counter_name == 'handover_success':
            return self.process.get_counter_stdout('HO successful')
        if counter_name == 'prach_sent':
            return self.process.get_counter_stdout('Random Access Transmission: seq=')
        if counter_name == 'prach_sent_nr':
            return self.process.get_counter_stdout('Random Access Transmission: prach_occasion=')
        if counter_name == 'paging_received':
            return self.process.get_counter_stdout('S-TMSI match in paging message')
        if counter_name == 'reestablishment_attempts':
            return self.process.get_counter_stdout('RRC Connection Reestablishment')
        if counter_name == 'reestablishment_ok':
            return self.process.get_counter_stdout('Reestablishment OK')
        if counter_name == 'rrc_connected_transitions':
            return self.process.get_counter_stdout('RRC Connected')
        if counter_name == 'rrc_idle_transitions':
            return self.process.get_counter_stdout('RRC IDLE')
        raise log.Error('counter %s not implemented!' % counter_name)

    def verify_metric(self, value, operation='avg', metric='dl_brate', criterion='gt', window=1):
        # copy back metrics if we have not already done so
        self.scp_back_metrics(self)
        metrics = srsUEMetrics(self.metrics_file)
        return metrics.verify(value, operation, metric, criterion, window)

numpy = None

class srsUEMetrics(log.Origin):

    VALID_OPERATIONS = ['avg', 'sum', 'max_rolling_avg', 'min_rolling_avg']
    VALID_CRITERION = ['eq','gt','lt']
    CRITERION_TO_SYM = { 'eq' : '==', 'gt' : '>', 'lt' : '<' }
    CRYTERION_TO_SYM_OPPOSITE = { 'eq' : '!=', 'gt' : '<=', 'lt' : '>=' }


    def __init__(self, metrics_file):
        super().__init__(log.C_RUN, 'srsue_metrics')
        self.raw_data = None
        self.metrics_file = metrics_file
        global numpy
        if numpy is None:
            import numpy as numpy_module
            numpy = numpy_module
        # read CSV, guessing data type with first row being the legend
        try:
            self.raw_data = numpy.genfromtxt(self.metrics_file, names=True, delimiter=';', dtype=None)
        except (ValueError, IndexError, IOError) as error:
            self.err("Error parsing metrics CSV file %s" % self.metrics_file)
            raise error

    def verify(self, value, operation='avg', metric_str='dl_brate', criterion='gt', window=1):
        if operation not in self.VALID_OPERATIONS:
            raise log.Error('Unknown operation %s not in %r' % (operation, self.VALID_OPERATIONS))
        if criterion not in self.VALID_CRITERION:
            raise log.Error('Unknown operation %s not in %r' % (operation, self.VALID_CRITERION))
        # check if given metric exists in data
        sel_data = numpy.array([])
        metrics_list = metric_str.split('+') # allow addition operator for columns
        for metric in metrics_list:
            try:
                vec = numpy.array(self.raw_data[metric])
            except ValueError as err:
                print('metric %s not available' % metric)
                raise err
            if sel_data.size == 0:
                # Initialize with dimension of first metric vector
                sel_data = vec
            else:
                # Sum them up assuming same array dimension
                sel_data += vec

        # Sum up all component carriers for rate metrics
        if metric_str.find('brate'):
            # Determine number of component carriers
            num_cc = numpy.amax(numpy.array(self.raw_data['cc'])) + 1 # account for zero index
            tmp_values = sel_data
            sel_data = numpy.array(tmp_values[::num_cc]) # first carrier, every num_cc'th item in list
            for cc in range(1, num_cc):
                sel_data += numpy.array(tmp_values[cc::num_cc]) # all other carriers, start at cc index

        if operation == 'avg':
            result = numpy.average(sel_data)
        elif operation == 'sum':
            result = numpy.sum(sel_data)
        elif operation == 'max_rolling_avg':
            # calculate rolling average over window and take maximum value
            result = numpy.amax(numpy.convolve(sel_data, numpy.ones((window,))/window, mode='valid'))
        elif operation == 'min_rolling_avg':
            # trim leading zeros to avoid false negative when UE attach takes longer
            sel_data = numpy.trim_zeros(sel_data, 'f')
            # calculate rolling average over window and take minimum value
            result = numpy.amin(numpy.convolve(sel_data, numpy.ones((window,))/window, mode='valid'))

        self.dbg(result=result, value=value)

        success = False
        if criterion == 'eq' and result == value or \
           criterion == 'gt' and result > value or \
           criterion == 'lt' and result < value:
            success = True

        # Convert bitrate in Mbit/s:
        if metric_str.find('brate') > 0:
            result /= 1e6
            value /= 1e6
            mbit_str = ' Mbit/s'
        else:
            mbit_str = ''

        if not success:
            result_msg = "{:.2f}{} {} {:.2f}{}".format(result, mbit_str, self.CRYTERION_TO_SYM_OPPOSITE[criterion], value, mbit_str)
            raise log.Error(result_msg)
        result_msg = "{:.2f}{} {} {:.2f}{}".format(result, mbit_str, self.CRITERION_TO_SYM[criterion], value, mbit_str)
        # TODO: overwrite test system-out with this text.
        return result_msg

# vim: expandtab tabstop=4 shiftwidth=4
