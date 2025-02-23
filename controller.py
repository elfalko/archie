from multiprocessing import Process
from multiprocessing import Manager
import subprocess
import pandas as pd
import argparse
import time
import prctl
try:
    import json5 as json
    print("Found JSON5 library")
except ModuleNotFoundError:
    import json
    pass

from faultclass import Fault
from faultclass import python_worker
from hdf5logger import hdf5collector
from goldenrun import run_goldenrun

import logging

clogger = logging.getLogger(__name__)


def build_ranges_dict(fault_dict):
    """
    build range, however allows to define type with a dict.
    """
    if fault_dict["type"] == "shift":
        ret = []
        if len(fault_dict["range"]) != 3:
            raise ValueError("For Shift 3 element list is needed")
        for i in range(fault_dict["range"][1], fault_dict["range"][2], 1):
            ret.append(fault_dict["range"][0] << i)
        return ret
    raise ValueError("No known type for this framework {}".format(fault_dict))


def build_ranges(fault_range):
    """
    build a range, if three elements are provided in a list. Otherwise build
    list with one element
    """
    if isinstance(fault_range, dict):
        return build_ranges_dict(fault_range)
    if len(fault_range) == 3:
        return range(fault_range[0], fault_range[1], fault_range[2])
    elif len(fault_range) == 1:
        return range(fault_range[0], fault_range[0] + 1, 1)
    else:
        clogger.critical("A provided range in the json is not valid. It is either a list of 1 or 3 elements. Provided was {}".format(fault_range))
        raise ValueError('Need 1 or 3 elements in list. Provided numbers were: {}'.format(fault_range))  # Need 1 or 3 elements in list


def detect_type(fault_type):
    """
    Translate type to enum value used in qemu
    """
    if fault_type == "flash" or fault_type == "instruction":
        return 1
    if fault_type == "sram" or fault_type == "data":
        return 0
    if fault_type == "register":
        return 2
    clogger.critical("Received wrong type. Expected instruction, data, or register. Got {}".format(fault_type))
    raise ValueError("A type was not detected. Maybe misspelled? got {} , needed instruction, data, or register".format(fault_type))


def detect_model(fault_model):
    """
    Translate model to enum value used in qemu
    """
    if fault_model == "set1":
        return 1
    if fault_model == "set0":
        return 0
    if fault_model == "toggle":
        return 2
    clogger.critical("Received wrong model. Expected set0, set1, or toggle. Got {}".format(fault_model))
    raise ValueError("A model was not detected. Maybe misspelled? got {} , needed set0 set1 toggle".format(fault_model))


def build_fault_list(conf_list, combined_faults, ret_faults):
    """
    Unrolling of multiple faults, that are combined. Will use recursive until
    no fault in list is remaining. Then build unrolled fault list, that has
    lists inside of faults executed together
    """
    ret_int_faults = ret_faults
    faultdev = conf_list.pop()
    if 'fault_livespan' in faultdev:
        faultdev['fault_lifespan'] = faultdev['fault_livespan']
    ftype = detect_type(faultdev['fault_type'])
    fmodel = detect_model(faultdev['fault_model'])
    for faddress in build_ranges(faultdev['fault_address']):
        for flifespan in build_ranges(faultdev['fault_lifespan']):
            for fmask in build_ranges(faultdev['fault_mask']):
                for taddress in build_ranges(faultdev['trigger_address']):
                    for tcounter in build_ranges(faultdev['trigger_counter']):
                        int_faults = combined_faults.copy()  # copy list, otherwise int fault referres to the same list as combined_faults
                        if faddress == -1:
                            faddress = taddress

                        int_faults.append(Fault(faddress, ftype,
                                                fmodel, flifespan,
                                                fmask, taddress,
                                                tcounter)
                                          )
                        if len(conf_list) == 0:
                            ret_int_faults.append(int_faults)
                        else:
                            ret_int_faults = build_fault_list(conf_list.copy(),
                                                              int_faults.copy(),
                                                              ret_faults)
    return ret_int_faults


def mem_limit_calc(mem_max, num_worker, queue_depth, time_max):
    if mem_max > 1500000:
        mem_estimate = mem_max * num_worker * 1.5 + queue_depth * mem_max
    else:
        mem_estimate = 1600000 * num_worker + queue_depth * mem_max
    time_max = 1 + time_max / 120.0
    mem_estimate = mem_estimate * time_max
    return mem_estimate


def get_system_ram():
    command = "cat /proc/meminfo"
    ps = subprocess.Popen(command,
                          shell=True,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    tmp = " "
    while ps.poll() is None:
        tmp = tmp + ps.stdout.read().decode('utf-8')
    sp = tmp.split('kB')
    t = sp[0]
    mem = int(t.split(':')[1], 0)
    clogger.info("system ram is {}kB".format(mem))
    return mem


def controller(hdf5path,
               hdf5mode,
               faultlist,
               config_qemu,
               num_workers,
               queuedepth,
               compressionlevel,
               qemu_output,
               goldenrun=True,
               logger=hdf5collector,
               qemu_pre=None,
               qemu_post=None,
               logger_postprocess=None):
    """
    This function builds the unrolled fault structure, performs golden run and
    then schedules the worker depending on ram usage and allowed number of
    workers
    """
    clogger.info("Controller start")
    t0 = time.time()
    m = Manager()
    m2 = Manager()
    q = m.Queue()
    q2 = m2.Queue()
    num_exp = len(faultlist)
    prctl.set_name("Controller")
    prctl.set_proctitle("Python_Controller")
    goldenrun_data = {}
    if goldenrun:
        [config_qemu['max_instruction_count'], goldenrun_data,
         faultlist] = run_goldenrun(config_qemu,
                                    qemu_output,
                                    q,
                                    faultlist,
                                    qemu_pre,
                                    qemu_post)
    p_logger = Process(target=logger,
                       args=(hdf5path,
                             hdf5mode,
                             q,
                             len(faultlist),
                             compressionlevel,
                             logger_postprocess)
                       )
    p_logger.start()
    p_list = []
    mem_list = []
    p_time_list = []
    p_time_list.append(60)
    p_time_mean = 60
    mem_max = 0
    max_ram = get_system_ram() * 0.9 - 2000000
    mem_list.append(max_ram / (num_workers))
    mem_max = max_ram/2
    time_max = 0
    goldenrun_data['tbexec'] = pd.DataFrame(goldenrun_data['tbexec'])
    goldenrun_data['tbinfo'] = pd.DataFrame(goldenrun_data['tbinfo'])
    goldenrun_data['meminfo'] = pd.DataFrame(goldenrun_data['meminfo'])
    if 'armregisters' in goldenrun_data:
        goldenrun_data['armregisters'] = pd.DataFrame(goldenrun_data['armregisters'])
    if 'riscvregisters' in goldenrun_data:
        goldenrun_data['riscvregisters'] = pd.DataFrame(goldenrun_data['riscvregisters'])
    itter = 0
    times = []
    while(1):
        len_p_list_cached = len(p_list)
        qsizecache = q.qsize()
        if len_p_list_cached < num_workers and mem_limit_calc(mem_max, len_p_list_cached, qsizecache, time_max) < max_ram:
            if len(faultlist) > itter and qsizecache < queuedepth:
                faults = faultlist[itter]
                itter += 1
                p = Process(name='worker_{}'.format(faults['index']),
                            target=python_worker,
                            args=(faults['faultlist'],
                                  config_qemu,
                                  faults['index'],
                                  q,
                                  qemu_output,
                                  goldenrun_data,
                                  True,
                                  q2,
                                  qemu_pre,
                                  qemu_post)
                            )
                p.start()
                p_context = {}
                p_context['process'] = p
                p_context['start_time'] = time.time()
                p_list.append(p_context)
                clogger.info("Started worker {}. Running: {}.".format(faults['index'],
                                                                      len_p_list_cached + 1))
            else:
                if len(p_list) == 0 and len(faultlist) == itter:
                    clogger.info("Done inserting qemu jobs")
                    break
                time.sleep(0.001)  # wait for queue to empty
        else:
            time.sleep(0.005)  # wait for workers to finish, scheduler can wait
        for i in range(0,  q2.qsize()):
            mem = q2.get_nowait()
            mem_list.append(mem)
        if len(mem_list) > 6 * num_workers + 4:
            del mem_list[0: len(mem_list)-6*num_workers+4]
        mem_max = max(mem_list)
        "Calculate length of running processes"
        times.clear()
        time_max = 0
        current_time = time.time()
        for i in range(0, len_p_list_cached):
            p = p_list[i]
            tmp = current_time - p['start_time']
            "If the current processing time is lower than moving average, do not punish the time "
            if tmp < p_time_mean:
                times.append(0)
            else:
                times.append(tmp - p_time_mean)
        """Find max time in list (This list will show the longest running
        process minus the moving average)"""
        if len(times) > 0:
            time_max = max(times)
        for i in range(0, len_p_list_cached):
            p = p_list[i]
            "Find finished processes"
            p['process'].join(timeout=0)
            if p['process'].is_alive() is False:
                "Recalculate moving average"
                p_time_list.append(current_time - p['start_time'])
                len_p_time_list = len(p_time_list)
                if len_p_time_list > num_workers + 2:
                    p_time_list.pop(0)
                p_time_mean = sum(p_time_list) / len_p_time_list
                clogger.info("Current running Average {}".format(p_time_mean))
                "Remove process from list"
                p_list.pop(i)
                break

    clogger.info("{} experiments remaining in queue".format(q.qsize()))
    p_logger.join()
    clogger.info("Done with qemu and logger")
    t1 = time.time()
    m, s = divmod(t1 - t0, 60)
    h, m = divmod(m, 60)
    clogger.info("Took {}:{}:{} to complet all experiments".format(h, m, s))
    tperindex = (t1-t0) / (num_exp)
    tperworker = (t1-t0) / (num_exp / num_workers)
    clogger.info("Took average of {}s per fault, python worker rough runtime is {}s".format(tperindex, tperworker))
    clogger.info("controller exit")
    return config_qemu


def get_argument_parser():
    parser = argparse.ArgumentParser(description='Read args for qemu fault injection tool')
    parser.add_argument('--qemu',
                        '-q',
                        help="Configuration for qemu. Needs to contain path to qemu, kernel and plugin in json format",
                        type=argparse.FileType('r', encoding='UTF-8'),
                        required=True)
    parser.add_argument('--faults',
                        '-f',
                        help="Faults for qemu. Needs to contain a valid config for faults",
                        type=argparse.FileType('r', encoding='UTF-8'),
                        required=True)
    parser.add_argument('--indexbase',
                        '-b',
                        help="Move index-base to arbitrary number. It is used in the hdf5 file",
                        type=int,
                        required=False)
    parser.add_argument('hdf5file', help="Destination of hdf5 file")
    parser.add_argument('--append',
                        '-a',
                        action="store_true",
                        help="append data to file instead of overwriting it",
                        required=False)
    parser.add_argument('--worker',
                        '-w',
                        help="Number of workers spawned. Default 1",
                        type=int,
                        required=False)
    parser.add_argument('--queuedepth',
                        help="Maximum number of elements in queue before scheduler blocks start of new workers. This allows to control the memory usage, default is 15",
                        type=int,
                        required=False)
    parser.add_argument('--compressionlevel',
                        '-c',
                        help="Set the compression level inside the hdf5 file. Valid values are between 0 to 9, 0 is no compression, 1 the highest, 9 the least. Default 1",
                        type=int,
                        required=False)
    parser.add_argument('--debug',
                        action="store_true",
                        help="This enables the output of qemu for debug purposes",
                        required=False)
    parser.add_argument('--gdb',
                        action="store_true",
                        help="Enables connection to the target with gdb. Port 1234",
                        required=False)
    return parser


def process_arguments(args):
    parguments = {}
    if args.append is False:
        parguments['hdf5mode'] = 'w'
        parguments['goldenrun'] = True
    else:
        parguments['hdf5mode'] = 'a'
        parguments['goldenrun'] = False

    indexbase = args.indexbase
    if args.indexbase is None:
        indexbase = 0

    parguments['num_workers'] = args.worker
    if args.worker is None:
        parguments['num_workers'] = 1

    parguments['queuedepth'] = args.queuedepth
    if args.queuedepth is None:
        parguments['queuedepth'] = 15

    parguments['compressionlevel'] = args.compressionlevel
    if args.compressionlevel is None:
        parguments['compressionlevel'] = 1

    qemu_conf = json.load(args.qemu)
    args.qemu.close()
    print(qemu_conf)
    if args.gdb:
        qemu_conf['gdb'] = True
        # hard set to 1 worker, because all qemus use the same port
        parguments['num_workers'] = 1

    faultlist = json.load(args.faults)
    if 'start' in faultlist:
        qemu_conf['start'] = faultlist['start']
    if 'end' in faultlist:
        qemu_conf['end'] = faultlist['end']

    if 'memorydump' in faultlist:
        qemu_conf['memorydump'] = faultlist['memorydump']
    if 'max_instruction_count' in faultlist:
        qemu_conf['max_instruction_count'] = faultlist['max_instruction_count']
    else:
        print("WARNING: missing max_instruction_count in json")
        qemu_conf['max_instruction_count'] = 100
    if 'tb_exec_list' in faultlist:
        qemu_conf['tb_exec_list'] = faultlist['tb_exec_list']
    if 'tb_info' in faultlist:
        qemu_conf['tb_info'] = faultlist['tb_info']
    if 'mem_info' in faultlist:
        qemu_conf['mem_info'] = faultlist['mem_info']

    parguments['qemu_conf'] = qemu_conf

    ret_list = []
    for faults in faultlist['faults']:
        tmp_list = []
        ret_list = build_fault_list(faults, tmp_list, ret_list)

    faultlist.clear()
    faultlist = []
    for i in range(len(ret_list)):
        faultconfig = {}
        faultconfig['index'] = i + indexbase
        faultconfig['faultlist'] = ret_list.pop()
        faultconfig['del'] = False
        faultlist.append(faultconfig)

    parguments['faultlist'] = faultlist
    return parguments


if __name__ == '__main__':
    """
    Main function to programm
    """

    parser = get_argument_parser()
    args = parser.parse_args()

    parguments = process_arguments(args)

    if args.debug:
        logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s : %(message)s', level=logging.DEBUG)
    else:
        logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s : %(message)s', level=logging.INFO)
    p = Process(target=controller,
                args=(args.hdf5file,                    # hdf5path
                      parguments['hdf5mode'],           # hdf5mode
                      parguments['faultlist'],          # faultlist
                      parguments['qemu_conf'],          # config_qemu
                      parguments['num_workers'],        # num_workers
                      parguments['queuedepth'],         # queuedepth
                      parguments['compressionlevel'],   # compressionlevel
                      args.debug,                       # qemu_output
                      parguments['goldenrun'],          # goldenrun
                      hdf5collector,                    # logger
                      None,                             # qemu_pre
                      None,                             # qemu_post
                      None)                             # logger_postprocess
                )
    p.start()
    p.join()
