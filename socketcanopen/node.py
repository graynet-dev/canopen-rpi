#TODO: OSErrors are thrown if the CAN bus goes down, need to do threaded Exception handling
#      See http://stackoverflow.com/questions/2829329/catch-a-threads-exception-in-the-caller-thread-in-python
#TODO: Check for BUS-OFF before attempting to send
#from datetime import datetime, timedelta
import logging
from select import select
import struct
from threading import Event, Thread, Timer, enumerate
from time import sleep, time

import socketcan
from .constants import *
from .indicators import *
from .messages import *
from .object_dictionary import *

logger = logging.getLogger(__name__)

class IntervalTimer(Thread):
    """Call a function every specified number of seconds:

            t = IntervalTimer(30.0, f, args=None, kwargs=None)
            t.start()
            t.cancel()    # stop the timer's action if it's still running
    """

    def __init__(self, interval, function, args=None, kwargs=None):
        super().__init__(args=args, kwargs=kwargs)
        self.interval = interval
        self.function = function
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}
        self.finished = Event()

    def cancel(self):
        self.finished.set()

    def run(self):
        while not self.finished.wait(self.interval):
            if self.finished.is_set():
                break
            self.function(*self.args, **self.kwargs)


class SdoAbort(Exception):
    def __init__(self, index, subindex, code):
        self.index = index
        self.subindex = subindex
        self.code = code


class SdoTimeout(Exception):
    pass


class Node:
    def __init__(self, bus: socketcan.Bus, id, od: ObjectDictionary, *args, **kwargs):
        self.bus = bus
        if id > 0x7F or id <= 0:
            raise ValueError("Invalid Node ID")
        self.id = id
        self._default_od = od

        if "err_indicator" in kwargs:
            if isinstance(kwargs["err_indicator"], ErrorIndicator):
                self._err_indicator = kwargs["err_indicator"]
                self._process_err_indicator()
                self._err_indicator_timer = IntervalTimer(self._err_indicator.interval , self._process_err_indicator)
                self._err_indicator_timer.start()
            else:
                raise TypeError
        else:
            self._err_indicator = None
            self._err_indicator_timer = None

        if "run_indicator" in kwargs:
            if isinstance(kwargs["run_indicator"], RunIndicator):
                self._run_indicator = kwargs["run_indicator"]
            else:
                raise TypeError
        else:
            self._run_indicator = None

        self._emcy_inhibit_time = 0
        self._first_boot = True
        self._heartbeat_consumer_timers = {}
        self._heartbeat_producer_timer = None
        self._listener = None
        self._message_timers = []
        self._nmt_active_master = False
        self._nmt_active_master_id = None
        self._nmt_active_master_timer = None
        self._nmt_flying_master_timer = None
        self._nmt_inhibit_time = 0
        self._nmt_multiple_master_timer = None
        self._pending_emcy_msgs = []
        self._sdo_data = None
        self._sdo_data_type = None
        self._sdo_len = None
        self._sdo_t = None
        self._sdo_odi = None
        self._sdo_odsi = None
        self._sync_counter = 0
        self._sync_timer = None
        self._timedelta = timedelta()
        self._tpdo_triggers = [False, False, False, False]

        self.nmt_state = NMT_STATE_INITIALISATION
        self.reset()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._reset_timers()

    def _boot(self):
        logger.info("Booting with node-ID of {}".format(self.id))
        self._send(BootupMessage(self.id))
        self.nmt_state = NMT_STATE_PREOPERATIONAL
        if self._listener is None:
            self._listener = Thread(target=self._listen, daemon=True)
            self._listener.start()
        else:
            self._is_listening = True
        self._process_timers()
        self._nmt_startup()

    @staticmethod
    def _cancel_timer(timer: Timer):
        if timer is not None and timer.is_alive():
            timer.cancel()
            return True
        return False

    def _heartbeat_consumer_timeout(self, id):
        self.emcy(EMCY_HEARTBEAT_BY_NODE + id)
        request_nmt_obj = self.od.get(ODI_REQUEST_NMT)
        if request_nmt_obj is not None:
            request_nmt_subobj = request_nmt_obj.get(id)
            request_nmt_subobj.value = 0x01 # CANopen device is missing
            request_nmt_obj.update({id: request_nmt_subobj})
            self.od.update({ODI_REQUEST_NMT: request_nmt_obj})

    def _listen(self):
        self._is_listening = True
        while True:
            msg = self.recv()
            self._process_msg(msg)

    def _nmt_active_master_timeout(self, first_boot=None):
        if first_boot is None:
            first_boot = self._first_boot
        elif first_boot is True:
            logger.info("Active NMT master failure detected")
        if first_boot:
            logger.debug("Active NMT master timeout after power-on")
            self._first_boot = False
            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_RESET_COMMUNICATION, 0))
            self._nmt_flying_master_startup()
        else:
            logger.debug("Active NMT master timeout after reboot")
            self._nmt_flying_master_negotiation_request()

    def _nmt_become_active_master(self):
        logger.info("Device is active NMT master")
        self._nmt_active_master = True
        # See CiA 302-2 v4.1.0, section 5.5.3
        nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
        self._cancel_timer(self._nmt_multiple_master_timer)
        nmt_multiple_master_detect_time = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_DETECT_TIME).value / 1000
        self._nmt_multiple_master_timer = IntervalTimer(nmt_multiple_master_detect_time, self._send, [NmtForceFlyingMasterRequest()])
        self._nmt_multiple_master_timer.start()

        mandatory_slaves = []
        reset_communication_slaves = []
        slaves_obj = self.od.get(ODI_NMT_SLAVE_ASSIGNMENT)
        if slaves_obj is not None:
            slaves_obj_length = slaves_obj.get(ODSI_VALUE).value
            for slave_id in range(1, slaves_obj_length + 1):
                slave = slaves_obj.get(slave_id).value
                if slave & 0x08:
                    mandatory_slaves += [slave_id]
                if (slave & 0x10) == 0:
                    reset_communication_slaves += [slave_id]
        if slaves_obj is None or len(reset_communication_slaves) == slaves_obj_length:
            logger.debug("No keep alive nodes, reset communication to all")
            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_RESET_COMMUNICATION, 0))
        else:
            for slave_id in reset_communication_slaves:
                logger.debug("Resetting communication for slave with node-ID {}".format(slave_id))
                self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_RESET_COMMUNICATION, slave_id))

        #Start process boot NMT slave
        all_nodes_not_booted = len(mandatory_slaves)
        self._nmt_boot_time_expired = False
        boot_time_obj = self.od.get(ODI_BOOT_TIME)
        if boot_time_obj is not None:
            self._nmt_boot_timer = Timer(boot_time_obj.get(ODSI_VALUE).value / 1000, self._nmt_boot_timeout)
        # TODO: Threaded Boot NMT slave
        mandatory_slaves_booted = 0
        while (len(mandatory_slaves) < mandatory_slaves_booted) or not self._nmt_boot_time_expired:
            mandatory_slaves_booted = 0
            request_nmt_obj = self.od.get(ODI_REQUEST_NMT)
            if request_nmt_obj is not None:
                for slave_id in mandatory_slaves:
                    slave_nmt_state = request_nmt_obj.get(slave_id)
                    if slave_nmt_state is not None and slave_nmt_state.value > 0x01:
                        mandatory_slaves_booted += 1
            sleep(0.25)
        #End process boot NMT slave

        request_nmt_obj = self.od.get(ODI_REQUEST_NMT)
        for slave_id in mandatory_slaves:
            if request_nmt_obj.get(slave_id).value <= 0x01:
                return # Halt startup procedure

        nmt_startup = self.od.get(ODI_NMT_STARTUP).get(ODSI_VALUE).value
        if (nmt_startup & 0x08) == 0:
            if nmt_startup & 0x01: # Start all nodes
                self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_START, 0))
            elif slaves_obj is not None:
                for slave_id in range(1, slaves_obj_length + 1):
                    self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_START, slave_id))
        if (nmt_startup & 0x04) == 0: # Self-starting
            self.nmt_state = NMT_STATE_OPERATIONAL

    def _nmt_become_inactive_master(self):
        logger.info("Device is not active NMT master, running in NMT slave mode")
        self._nmt_active_master = False
        self._cancel_timer(self._nmt_active_master_timer)
        if self._nmt_active_master_id not in self._heartbeat_consumer_timers: # See CiA 302-2 v4.1.0, section 5.5.2
            logger.debug("Active NMT master not in heartbeat consumers; timeout will be twice heartbeat producer time")
            heartbeat_producer_time = self.od.get(ODI_HEARTBEAT_PRODUCER_TIME).get(ODSI_VALUE).value
            self._nmt_active_master_timer = Timer(heartbeat_producer_time * 2 / 1000, self._nmt_active_master_timeout, [True])
            self._nmt_active_master_timer.start()

    def _nmt_boot_timeout(self):
        self._nmt_boot_time_expired = True

    def _nmt_compare_flying_master_priority(self, priority):
        nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
        own_priority = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY).value
        if priority <= own_priority:
            logger.debug("Acive NMT Master priority level is the same or higher")
            self._nmt_become_inactive_master()
        else:
            logger.debug("Acive NMT Master priority level is lower")
            self.send_nmt(NmtForceFlyingMasterRequest())
            self._nmt_flying_master_startup()

    def _nmt_flying_master_negotiation(self):
        nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
        priority = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY).value
        priority_time_slot = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY_TIME_SLOT).value
        device_time_slot = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_DEVICE_TIME_SLOT).value
        self._cancel_timer(self._nmt_flying_master_timer)
        flying_master_response_wait_time = (priority * priority_time_slot + self.id * device_time_slot) / 1000
        self._nmt_flying_master_timer = Timer(flying_master_response_wait_time, self._nmt_flying_master_negotiation_timeout)
        self._nmt_flying_master_timer.start()

    def _nmt_flying_master_negotiation_request(self):
        logger.debug("Requesting service NMT flying master negotiaion")
        self.send_nmt(NmtFlyingMasterRequest())
        self._nmt_flying_master_negotiation()

    def _nmt_flying_master_negotiation_timeout(self):
        logger.debug("NMT flying master negotiaion timeout")
        nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
        priority = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY).value
        self.send_nmt(NmtMasterNodeIdMessage(priority, self.id))
        self._nmt_become_active_master()

    def _nmt_flying_master_startup(self):
        logger.debug("Entering NMT flying master process")
        flying_master_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
        if flying_master_params is None:
            raise RuntimeException("Device is configured as NMT flying master, but object dictionary parameters do not exist")
        flying_master_negotiation_delay = flying_master_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_DELAY).value
        sleep(flying_master_negotiation_delay / 1000)
        logger.debug("Service active NMT master detection")
        self.send_nmt(NmtActiveMasterRequest())
        self._cancel_timer(self._nmt_active_master_timer)
        active_nmt_master_timeout_time = flying_master_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_TIMEOUT).value / 1000
        self._nmt_active_master_timer = Timer(active_nmt_master_timeout_time, self._nmt_active_master_timeout)
        self._nmt_active_master_timer.start()

    def _nmt_startup(self):
        logger.debug("Entering NMT startup process")
        nmt_startup_obj = self.od.get(ODI_NMT_STARTUP)
        if nmt_startup_obj is not None:
            nmt_startup = nmt_startup_obj.get(ODSI_VALUE).value
            if nmt_startup & 0x01: # NMT Master
                if nmt_startup & 0x20: # NMT Flying Master
                    self._nmt_flying_master_startup()
                else:
                    self._nmt_become_active_master()
            else:
                if (nmt_startup & 0x04) == 0: # Self-starting
                    self.nmt_state = NMT_STATE_OPERATIONAL
                logger.debug("Entering NMT slave mode")

    def _process_err_indicator(self):
        err_state = self.bus.get_state()
        self._err_indicator.set_state(err_state)

    def _process_heartbeat_producer(self):
        heartbeat_producer_time_object = self.od.get(ODI_HEARTBEAT_PRODUCER_TIME)
        if heartbeat_producer_time_object is not None:
            heartbeat_producer_time_value = heartbeat_producer_time_object.get(ODSI_VALUE)
            if heartbeat_producer_time_value is not None and heartbeat_producer_time_value.value is not None:
                heartbeat_producer_time = heartbeat_producer_time_value.value / 1000
            else:
                heartbeat_producer_time = 0
        else:
            heartbeat_producer_time = 0
        self._cancel_timer(self._heartbeat_producer_timer)
        if heartbeat_producer_time != 0:
            self._heartbeat_producer_timer = IntervalTimer(heartbeat_producer_time, self._send_heartbeat)
            self._heartbeat_producer_timer.start()

    def _process_msg(self, msg: socketcan.Message):
        if not self.is_listening:
            return
        can_id = msg.arbitration_id
        data = msg.data
        fc = (can_id & FUNCTION_CODE_MASK) >> FUNCTION_CODE_BITNUM # Only look for restricted CAN-IDs using function code
        if msg.is_remote_frame: # CiA recommendeds against using RTRs, but they are still supported
            target_node = msg.node_id
            if target_node == self.id or target_node == BROADCAST_NODE_ID:
                if self.nmt_state == NMT_STATE_OPERATIONAL:
                    tpdo = None
                    # TODO: Lookup TPDO CAN-IDs from OD
                    if fc == FUNCTION_CODE_TPDO1:
                        tpdo = 1
                    elif fc == FUNCTION_CODE_TPDO2:
                        tpdo = 2
                    elif fc == FUNCTION_CODE_TPDO3:
                        tpdo = 3
                    elif fc == FUNCTION_CODE_TPDO4:
                        tpdo = 4
                    if tpdo is not None:
                        tpdo_cp = self.od.get(ODI_TPDO1_COMMUNICATION_PARAMETER + tpdo - 1)
                        if tpdo_cp is not None:
                            tpdo_cp_id = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_ID)
                            if tpdo_cp_id is not None and (tpdo_cp_id >> TPDO_COMM_PARAM_ID_VALID_BITNUM) & 1 == 0 and (tpdo_cp_id >> TPDO_COMM_PARAM_ID_RTR_BITNUM) & 1 == 0:
                                tpdo_cp_type = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_TYPE)
                                if tpdo_cp_type == 0xFC:
                                    self._tpdo_triggers[0] = True; # Defer until SYNC event
                                elif tpdo_cp_type == 0xFD:
                                    self._send_pdo(tpdo)
                elif fc == FUNCTION_CODE_NMT_ERROR_CONTROL:
                    self._send_heartbeat()
        elif fc == FUNCTION_CODE_NMT:
            command = can_id & 0x7F
            if command == NMT_NODE_CONTROL:
                target_node = data[1]
                if target_node == self.id or target_node == BROADCAST_NODE_ID:
                    cs = data[0]
                    if cs == NMT_NODE_CONTROL_START:
                        self.nmt_state = NMT_STATE_OPERATIONAL
                    elif cs == NMT_NODE_CONTROL_STOP:
                        self.nmt_state = NMT_STATE_STOPPED
                    elif cs == NMT_NODE_CONTROL_PREOPERATIONAL:
                        self.nmt_state = NMT_STATE_PREOPERATIONAL
                    elif cs == NMT_NODE_CONTROL_RESET_NODE:
                        self.reset()
                    elif cs == NMT_NODE_CONTROL_RESET_COMMUNICATION:
                        logger.debug("Received reset communication")
                        self.reset_communication()
            elif command == NMT_MASTER_NODE_ID: # Response from either an NmtActiveMasterRequest, NmtFlyingMasterRequest, or unsolicted from non-Flying Master after bootup was indicated
                if self.is_nmt_master_capable:
                    logger.debug("Active NMT flying master detected with node-ID {}".format(data[1]))
                    compare_priority = False
                    self._nmt_active_master_id = data[1]
                    if self._cancel_timer(self._nmt_active_master_timer): # If from NmtActiveMasterRequest
                        self._first_boot = False
                        compare_priority = True
                    if self._cancel_timer(self._nmt_flying_master_timer): # If from NmtFlyingMasterRequest
                        compare_priority = True
                    if self._cancel_timer(self._nmt_multiple_master_timer):
                        compare_priority = True
                    if compare_priority:
                        self._nmt_compare_flying_master_priority(data[0])
            elif command == NMT_ACTIVE_MASTER_REQUEST:
                if self.is_active_nmt_master:
                    nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
                    priority = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY).value
                    self.send_nmt(NmtMasterNodeIdMessage(priority, self.id))
            elif command == NMT_FLYING_MASTER_REQUEST:
                nmt_startup_obj = self.od.get(ODI_NMT_STARTUP)
                if nmt_startup_obj is not None:
                    nmt_startup = nmt_startup_obj.get(ODSI_VALUE).value
                    if nmt_startup & 0x21: # Is NMT Flying Master
                        self._nmt_flying_master_negotiation()
            elif command == NMT_MASTER_REQUEST:
                if self.is_nmt_master_capable:
                 self.send_nmt(NmtMasterResponse())
            elif command == NMT_FORCE_FLYING_MASTER:
                if self.is_nmt_master_capable:
                    logger.info("Force NMT flying master negotation service indicated")
                    self._nmt_become_inactive_master()
                    self._nmt_flying_master_startup()
        elif fc == FUNCTION_CODE_NMT_ERROR_CONTROL:
            producer_id = can_id & 0x7F
            producer_nmt_state = data[0]
            if producer_id in self._heartbeat_consumer_timers:
                self._cancel_timer(self._heartbeat_consumer_timers.get(producer_id))
            elif self.is_nmt_master_capable and (producer_id == self._nmt_active_master_id):
                self._cancel_timer(self._nmt_active_master_timer)
                heartbeat_producer_object = self.od.get(ODI_HEARTBEAT_PRODUCER_TIME)
                if heartbeat_producer_object is not None:
                    heartbeat_producer_value = heartbeat_producer_object.get(ODSI_VALUE)
                    if heartbeat_producer_value is not None and heartbeat_producer_value.value != 0:
                        self._nmt_active_master_timer = Timer(heartbeat_producer_value.value * 1.5 / 1000, self._nmt_active_master_timeout, [True])
                        self._nmt_active_master_timer.start()

            heartbeat_consumer_time = 0
            heartbeat_consumer_time_object = self.od.get(ODI_HEARTBEAT_CONSUMER_TIME)
            if heartbeat_consumer_time_object is not None:
                heartbeat_consumer_time_length = heartbeat_consumer_time_object.get(ODSI_VALUE)
                if heartbeat_consumer_time_length is not None and heartbeat_consumer_time_length.value is not None:
                    for i in range(1, heartbeat_consumer_time_length.value + 1):
                        heartbeat_consumer_time_value = heartbeat_consumer_time_object.get(i)
                        if heartbeat_consumer_time_value is not None and heartbeat_consumer_time_value.value is not None and ((heartbeat_consumer_time_value.value >> 16) & 0x7F) == producer_id:
                            heartbeat_consumer_time = (heartbeat_consumer_time_value.value & 0xFFFF) / 1000
                            break
            if heartbeat_consumer_time != 0:
                heartbeat_consumer_timer = Timer(heartbeat_consumer_time, self._heartbeat_consumer_timeout, [producer_id])
                heartbeat_consumer_timer.start()
                self._heartbeat_consumer_timers.update({producer_id: heartbeat_consumer_timer})
                if self.is_nmt_master_capable and (producer_id == self._nmt_active_master_id):
                    self._cancel_timer(self._nmt_active_master_timer)
                    self._nmt_active_master_timer = Timer(heartbeat_consumer_time, self._nmt_active_master_timeout)
                    self._nmt_active_master_timer.start()
            request_nmt_obj = self.od.get(ODI_REQUEST_NMT)
            if request_nmt_obj is not None:
                request_nmt_subobj = request_nmt_obj.get(producer_id)
                if request_nmt_subobj is not None:
                    request_nmt_subobj.value = NMT_STATE_PREOPERATIONAL if producer_nmt_state == NMT_STATE_INITIALISATION else producer_nmt_state
                    request_nmt_obj.update({producer_id: request_nmt_subobj})
                    self.od.update({ODI_REQUEST_NMT: request_nmt_obj})

            # Service NMT master node-ID
            if self.is_active_nmt_master and producer_nmt_state == NMT_STATE_INITIALISATION:
                nmt_flying_master_timing_params = self.od.get(ODI_NMT_FLYING_MASTER_TIMING_PARAMETERS)
                if nmt_flying_master_timing_params is not None:
                    priority = nmt_flying_master_timing_params.get(ODSI_NMT_FLYING_MASTER_TIMING_PARAMS_PRIORITY).value
                else:
                    priority = 0
                self.send_nmt(NmtMasterNodeIdMessage(priority, self.id))

        else: # Check non-restricted CAN-IDs
            if self.nmt_state == NMT_STATE_OPERATIONAL:
                sync_obj = self.od.get(ODI_SYNC)
                if sync_obj is not None:
                    sync_obj_value = sync_obj.get(ODSI_VALUE)
                    if sync_obj_value is not None and (sync_obj_value.value & 0x1FFFFFFF) == can_id:
                        self._sync_counter = (self._sync_counter + 1) % 241
                        for i in range(4):
                            tpdo_cp = self.od.get(ODI_TPDO1_COMMUNICATION_PARAMETER + i)
                            if tpdo_cp is not None:
                                tpdo_cp_id = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_ID)
                                if tpdo_cp_id is not None and tpdo_cp_id.value is not None and (tpdo_cp_id.value >> TPDO_COMM_PARAM_ID_VALID_BITNUM) & 1 == 0:
                                    tpdo_cp_type = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_TYPE)
                                    if tpdo_cp_type is not None and tpdo_cp_type.value is not None and (((tpdo_cp_type.value == 0 or tpdo_cp_type.value == 0xFC) and self._tpdo_triggers[i]) or (self._sync_counter % tpdo_cp_type.value) == 0):
                                        self._send_pdo(i + 1)
            if self.nmt_state != NMT_STATE_STOPPED:
                time_obj = self.od.get(ODI_TIME_STAMP)
                if time_obj is not None:
                    time_cob_id = time_obj.get(ODSI_VALUE).value
                    if time_cob_id & 0x80 and time_cob_id & 0x1FFFF == can_id:
                        ms, d = struct.unpack("<IH", data[0:6])
                        ms = ms >> 4
                        td = timedelta(days=d, milliseconds=ms)
                        ts = datetime(1980, 1, 1) + td
                        self._timedelta = ts - datetime.now()
            if self.nmt_state != NMT_STATE_STOPPED and len(data) == 8: # Ignore SDO if data is not 8 bytes
                sdo_server_object = self.od.get(ODI_SDO_SERVER)
                if sdo_server_object is not None:
                    sdo_server_csid = sdo_server_object.get(ODSI_SDO_SERVER_DEFAULT_CSID)
                    if sdo_server_csid is not None and (sdo_server_csid.value & 0x1FFFFFFF) == can_id:
                        try:
                            ccs = (data[0] & SDO_CS_MASK) >> SDO_CS_BITNUM
                            if ccs in [SDO_CCS_DOWNLOAD_INITIATE, SDO_CCS_UPLOAD_INITIATE]:
                                odi = (data[2] << 8) + data[1]
                                odsi = data[3]
                                if odi in self.od:
                                    obj = self.od.get(odi)
                                    if odsi in obj:
                                        subobj = obj.get(odsi)
                                    else:
                                        raise SdoAbort(odi, odsi, SDO_ABORT_SUBINDEX_DNE)
                                else:
                                    raise SdoAbort(odi, odsi, SDO_ABORT_OBJECT_DNE)
                            if ccs == SDO_CCS_DOWNLOAD_INITIATE:
                                if subobj.access_type in [AccessType.RO, AccessType.CONST]:
                                    raise SdoAbort(odi, odsi, SDO_ABORT_RO)
                                scs = SDO_SCS_DOWNLOAD_INITIATE
                                s = (data[0] >> SDO_S_BITNUM) & 1
                                e = (data[0] >> SDO_E_BITNUM) & 1
                                data_type_index = subobj.data_type
                                if e == 1 and s == 1:
                                    n = (data[0] & SDO_INITIATE_N_MASK) >> SDO_INITIATE_N_BITNUM
                                    subobj.value = subobj.from_bytes(data[4:8-n])
                                elif e == 1 and s == 0:
                                    n = 0 # Unspecified number of bytes, default to all
                                    if data_type_index in self.od:
                                        data_type_object = self.od.get(data_type_index)
                                        if ODSI_VALUE in data_type_object:
                                            n = 4 - max(1, data_type_object.get(ODSI_VALUE).value // 8)
                                    subobj.value = subobj.from_bytes(data[4:8-n])
                                elif e == 0 and s == 1: # Normal (non-expedited) SDO
                                    self._sdo_odi = odi
                                    self._sdo_odsi = odsi
                                    self._sdo_t = 0
                                    self._sdo_len = int.from_bytes(data[4:8], byteorder='little')
                                    self._sdo_data = []
                                    self._sdo_data_type = data_type_index
                                else: # e == 0, s == 0 is reserved
                                    logger.error("SDO Download Initiate Request with e=0 & s=0 aborted")
                                    raise SdoAbort(odi, odsi, SDO_ABORT_GENERAL)
                                if e == 1: # Handle special cases
                                    if odi == ODI_PREDFINED_ERROR_FIELD and subobj.value != 0:
                                        raise SdoAbort(odi, odsi, SDO_ABORT_INVALID_VALUE)
                                    if odi == ODI_REQUEST_NMT:
                                        if not self.is_active_nmt_master:
                                            logger.error("SDO Download to NMT Request aborted; device is not active NMT master")
                                            raise SdoAbort(odi, odsi, SDO_ABORT_GENERAL)
                                        target_node = odsi & 0x7F
                                        if (subobj.value & 0x7F) == 0x04: # Stop remote node
                                            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_STOP, target_node))
                                        elif (subobj.value & 0x7F) == 0x05: # Start remote node
                                            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_START, target_node))
                                        elif (subobj.value & 0x7F) == 0x06: # Reset node
                                            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_RESET_NODE, target_node))
                                        elif (subobj.value & 0x7F) == 0x06: # Reset communication
                                            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_RESET_COMMUNICATION, target_node))
                                        elif (subobj.value & 0x7F) == 0x06: # Enter preoperational
                                            self.send_nmt(NmtNodeControlMessage(NMT_NODE_CONTROL_PREOPERATIONAL, target_node))
                                        else:
                                            raise SdoAbort(odi, odsi, SDO_ABORT_INVALID_VALUE)
                                obj.update({odsi: subobj}) # TODO: Don't update for some special cases (above)
                                self.od.update({odi: obj})
                                self._process_timers() # Update timers since OD was modified
                                data = struct.pack("<BHB4x", scs << SDO_CS_BITNUM, odi, odsi)
                            elif ccs == SDO_CCS_DOWNLOAD_SEGMENT:
                                if self._sdo_data is None:
                                    logger.error("SDO Download Segment Request aborted, initate not received or aborted")
                                    raise SdoAbort(0, 0, SDO_ABORT_GENERAL) # Initiate not receieved or aborted
                                scs = SDO_SCS_DOWNLOAD_SEGMENT
                                t = (data[0] >> SDO_T_BITNUM) & 1
                                if self._sdo_t != t:
                                    raise SdoAbort(self._sdo_odi, self._sdo_odsi, SDO_ABORT_TOGGLE)
                                self._sdo_t = t ^ 1
                                n = (data[0] & SDO_SEGMENT_N_MASK) >> SDO_SEGMENT_N_BITNUM
                                self._sdo_data += data[1:8-n]
                                c = (data[0] >> SDO_C_BITNUM) & 1
                                if c == 1:
                                    obj = self.od.get(self._sdo_odi)
                                    subobj = obj.get(self._sdo_odsi)
                                    subobj.value = subobj.from_bytes(self._sdo_data)
                                    obj.update({self._sdo_odsi: subobj})
                                    self.od.update({self._sdo_odi: obj})
                                    self._process_timers() # Update timers since OD was modified
                                    self._sdo_data = None
                                    self._sdo_data_type = None
                                    self._sdo_len = None
                                    self._sdo_t = None
                                    self._sdo_odi = None
                                    self._sdo_odsi = None
                                data = struct.pack("<B7x", (scs << SDO_CS_BITNUM) + (t << SDO_T_BITNUM) + (n << SDO_SEGMENT_N_BITNUM) + (c << SDO_C_BITNUM))
                            elif ccs == SDO_CCS_UPLOAD_INITIATE:
                                if subobj.access_type == AccessType.WO:
                                    raise SdoAbort(odi, odsi, SDO_ABORT_WO)
                                if odsi != ODSI_VALUE and obj.get(ODSI_VALUE).value < odsi:
                                    raise SdoAbort(odi, odsi, SDO_ABORT_NO_DATA)
                                scs = SDO_SCS_UPLOAD_INITIATE
                                data_type_index = subobj.data_type
                                data_type_length = None
                                if data_type_index in self.od:
                                    data_type_object = self.od.get(data_type_index)
                                    if ODSI_VALUE in data_type_object:
                                            data_type_length = data_type_object.get(ODSI_VALUE).value // 8
                                if data_type_length is None: # Unknown data length, default to expedited 4 bytes
                                    n = 0
                                    s = 0
                                    e = 1
                                    sdo_data = bytes(subobj)
                                elif data_type_length > 4:
                                    self._sdo_data = bytes(subobj)
                                    self._sdo_len = data_type_length
                                    self._sdo_t = 0
                                    self._sdo_odi = odi
                                    self._sdo_odsi = odsi
                                    s = 1
                                    e = 0
                                    n = 0
                                    sdo_data = struct.pack("<I", data_type_length)
                                else:
                                    n = 4 - data_type_length
                                    s = 1
                                    e = 1
                                    sdo_data = bytes(subobj)
                                data = struct.pack("<BHB4s", (scs << SDO_CS_BITNUM) + (n << SDO_INITIATE_N_BITNUM) + (e << SDO_E_BITNUM), odi, odsi, sdo_data)
                            elif ccs == SDO_CCS_UPLOAD_SEGMENT:
                                if self._sdo_data is None:
                                    logger.error("SDO Upload Initiate Request aborted, initiate not received or aborted")
                                    raise SdoAbort(0, 0, SDO_ABORT_GENERAL) # Initiate not receieved or aborted
                                scs = SDO_SCS_UPLOAD_SEGMENT
                                t = (data[0] >> SDO_T_BITNUM) & 1
                                if self._sdo_t != t:
                                    raise SdoAbort(self._sdo_odi, self._sdo_odsi, SDO_ABORT_TOGGLE)
                                self._sdo_t = t ^ 1
                                if self._sdo_len > 7:
                                    l = 7
                                else:
                                    l = self._sdo_len
                                sdo_data = self._sdo_data[-self._sdo_len:(-self._sdo_len+l or None)]
                                self._sdo_len -= l
                                n = 7 - l
                                if self._sdo_len > 0:
                                    c = 0
                                else:
                                    self._sdo_data = None
                                    self._sdo_len = None
                                    self._sdo_t = None
                                    c = 1
                                data = struct.pack("<B{}s".format(len(sdo_data)), (scs << SDO_CS_BITNUM) + (t << SDO_T_BITNUM) + (n << SDO_SEGMENT_N_BITNUM) + (c << SDO_C_BITNUM), sdo_data)
                            else:
                                logger.error("SDO Request aborted, invalid cs")
                                raise SdoAbort(odi, odsi, SDO_ABORT_INVALID_CS)
                        except SdoAbort as a:
                            self._sdo_data = None
                            self._sdo_len = None
                            self._sdo_t = None
                            self._sdo_odi = None
                            self._sdo_odsi = None
                            scs = SDO_CS_ABORT
                            data = struct.pack("<BHBI", scs << SDO_CS_BITNUM, a.index, a.subindex, a.code)
                        sdo_server_scid = sdo_server_object.get(ODSI_SDO_SERVER_DEFAULT_SCID)
                        if sdo_server_scid is None:
                            raise ValueError("SDO Server SCID not specified")
                        msg = socketcan.Message(sdo_server_scid.value & 0x1FFFFFFF, data)
                        self._send(msg)

    def _process_sync(self):
        sync_object = self.od.get(ODI_SYNC)
        if sync_object is not None:
            sync_object_value = sync_object.get(ODSI_VALUE)
            if sync_object_value is not None and sync_object_value.value is not None:
                is_sync_producer = (sync_object_value.value & 0x40000000) != 0
            else:
                is_sync_producer = False
        else:
            is_sync_producer = False
        sync_time_object = self.od.get(ODI_SYNC_TIME)
        if sync_time_object is not None:
            sync_time_value = sync_time_object.get(ODSI_VALUE)
            if sync_time_value is not None and sync_time_value.value is not None:
                sync_time = sync_time_value.value / 1000000
        else:
            sync_time = 0
        self._cancel_timer(self._sync_timer)
        if is_sync_producer and sync_time != 0 and self.nmt_state != NMT_STATE_STOPPED:
            self._sync_timer = IntervalTimer(sync_time, self._send_sync)
            self._sync_timer.start()

    def _process_timers(self):
        self._process_heartbeat_producer()
        self._process_sync()

    def _reset_timers(self):
        for t in self._message_timers:
            self._cancel_timer(t)
        for i, t in self._heartbeat_consumer_timers.items():
            self._cancel_timer(t)
        self._heartbeat_consumer_timers = {}
        self._cancel_timer(self._err_indicator_timer)
        self._cancel_timer(self._heartbeat_producer_timer)
        self._cancel_timer(self._sync_timer)
        self._cancel_timer(self._nmt_active_master_timer)
        self._cancel_timer(self._nmt_flying_master_timer)
        self._cancel_timer(self._nmt_multiple_master_timer)

    def _send(self, msg: socketcan.Message):
        return self.bus.send(msg)

    def _send_emcy(self, eec, msef=0):
        emcy_id_obj = self.od.get(ODI_EMCY_ID)
        if emcy_id_obj is None:
            return
        emcy_id_value = emcy_id_obj.get(ODSI_VALUE)
        if emcy_id_value is None:
            return
        if emcy_id_value.value is None:
            return
        er_obj = self.od.get(ODI_ERROR)
        if er_obj is None:
            return
        er_value = er_obj.get(ODSI_VALUE)
        if er_value is None:
            return
        if er_value.value is None:
            return
        msg = EmcyMessage(emcy_id_value.value, eec, er_value.value, msef)
        if self.nmt_state == NMT_STATE_STOPPED:
            self._pending_emcy_msgs.append(msg)
            return
        emcy_inhibit_time_obj = self.od.get(ODI_INHIBIT_TIME_EMCY)
        if emcy_inhibit_time_obj is not None:
            emcy_inhibit_time_subobj = emcy_inhibit_time_obj.get(ODSI_VALUE)
            if emcy_inhibit_time_subobj.value != 0:
                emcy_inhibit_time = emcy_inhibit_time_subobj.value / 10000
                if self._emcy_inhibit_time + emcy_inhibit_time < time():
                    logger.info("EMCY inhibit time violation, delaying message")
                    self._emcy_inhibit_time += emcy_inhibit_time
                    t = Timer(time() - self._emcy_inhibit_time, self.send_emcy, [eec, msef])
                    t.start()
                    self._message_timers.append(t)
                    return
        self._send(msg)

    def _send_heartbeat(self):
        msg = HeartbeatMessage(self.id, self.nmt_state)
        return self._send(msg)

    def _send_pdo(self, i):
        i = i - 1
        data = bytes()
        tpdo_mp = self.od.get(ODI_TPDO1_MAPPING_PARAMETER + i)
        if tpdo_mp is not None:
            tpdo_mp_length = tpdo_mp.get(ODSI_VALUE)
            if tpdo_mp_length is not None and tpdo_mp_length.value is not None:
                for j in range(tpdo_mp_length.value):
                    mapping_param = tpdo_mp.get(j + 1)
                    if mapping_param is not None and mapping_param.value is not None:
                        mapped_obj = self.od.get(mapping_param.value >> 16)
                        if mapped_obj is not None:
                             mapped_subobj = mapped_obj.get((mapping_param.value >> 8) & 0xFF)
                        else:
                            raise ValueError("Mapped PDO object does not exist")
                        if mapped_subobj is not None and mapped_subobj.value is not None:
                            mapped_bytes = bytes(subobj)
                            if len(mapped_bytes) != ((mapping_param.value & 0xFF) // 8):
                                raise ValueError("PDO Mapping length mismatch")
                            data = data + mapped_bytes
                    else:
                        raise ValueError("Mapped PDO object does not exist")
                msg = PdoMessage(FUNCTION_CODE_TPDO1 + (2 * i), self.id, data)
                self._send(msg)
                self._tpdo_triggers[i] = False

    def _send_sync(self):
        sync_object = self.od.get(ODI_SYNC)
        if sync_object is not None:
            sync_value = sync_object.get(ODSI_VALUE)
            if sync_value is not None and sync_value.value is not None:
                sync_id = sync_value.value & 0x1FFFF
                msg = socketcan.Message(sync_id)
                self._send(msg)

    def emcy(self, eec, msef=0):
        errors_obj = self.od.get(ODI_PREDEFINED_ERROR_FIELD)
        if errors_obj is not None:
            errors_length_subobj = errors_obj.get(ODSI_VALUE)
            errors_length_subobj.value = max(0xFF, errors_length_subobj.value + 1)
            errors_obj.update({ODSI_VALUE: errors_length_subobj})
            for si in range(1, errors_length_subobj.value):
                errors_obj.update({(si + 1): errors_obj.get(si)})
            errors_obj.update({0x01: SubObject(
                parameter_name="Standard error field",
                access_type=socketcanopen.AccessType.RO,
                data_type=socketcanopen.ODI_DATA_TYPE_UNSIGNED32,
                low_limit=0x00000000,
                high_limit=0xFFFFFFF,
                default_value=((msef & 0xFFFF) << 16) + eec
            )})
            self.od.update({ODI_PREDEFINED_ERROR_FIELD: errors_obj})
        self._send_emcy(eec, msef)

    @property
    def is_listening(self):
        return self._is_listening

    @property
    def nmt_state(self):
        return self._nmt_state

    @nmt_state.setter
    def nmt_state(self, nmt_state):
        self._nmt_state = nmt_state
        try:
            self._run_indicator.set_state(nmt_state)
        except AttributeError:
            pass
        for msg in self._pending_emcy_msgs:
            self.send_emcy(msg)

    @property
    def is_active_nmt_master(self):
        return self._nmt_active_master

    @property
    def is_nmt_master_capable(self):
        nmt_startup_obj = self.od.get(ODI_NMT_STARTUP)
        if nmt_startup_obj is not None:
            nmt_startup = nmt_startup_obj.get(ODSI_VALUE).value
            if nmt_startup & 0x01:
                return True
        return False

    def recv(self):
        while True:
            rlist, _, _, = select([self.bus], [], [])
            if len(rlist) > 0:
                #msg =  Message.factory(self.bus.recv()) # Message.factory uses default COB-IDs
                msg = self.bus.recv() # Returns socketcan.Message
                return msg

    def reset(self):
        logger.info("Device reset")
        self._is_listening = False
        self.od = self._default_od
        self.reset_communication()

    def reset_communication(self):
        logger.info("Device reset communication")
        self._reset_timers()
        if self._err_indicator is not None:
            self._err_indicator_timer = IntervalTimer(self._err_indicator.interval, self._process_err_indicator)
            self._err_indicator_timer.start()
        for odi, obj in self._default_od.items():
            if odi >= 0x1000 and odi <= 0x1FFF:
                self.od.update({odi: obj})
        self._pending_emcy_msgs = []
        self._boot()

    def reset_emcy(self):
        self._send_emcy(0)

    def send_nmt(self, msg):
        nmt_inhibit_time_obj = self.od.get(ODI_NMT_INHIBIT_TIME)
        if nmt_inhibit_time_obj is not None:
            nmt_inhibit_time_subobj = nmt_inhibit_time_obj.get(ODSI_VALUE)
            if nmt_inhibit_time_subobj.value != 0:
                nmt_inhibit_time = nmt_inhibit_time_subobj.value / 1000
                if self._nmt_inhibit_time + nmt_inhibit_time < time():
                    logger.info("NMT inhibit time violation, delaying message")
                    self._nmt_inhibit_time += nmt_inhibit_time
                    t = Timer(time() - self._nmt_inhibit_time, self._send, [msg])
                    t.start()
                    self._message_timers.append(t)
                    return
        return self._send(msg)

    def send_time(self, ts=None):
        if ts is None:
            ts = datetime.now() + self._timedelta
        if not isinstance(ts, datetime):
            raise ValueError("Timestamp must be of type datetime")
        time_obj = self.od.get(ODI_TIME_STAMP)
        if time_obj is None:
            return False
        time_cob_id = time_obj.get(ODSI_value).value
        if time_cob_id & 0x40:
            td = ts - datetime(1984, 1, 1)
            self._send(Message(time_cob_id & 0x1FFF, self.id, struct.pack("<IH", int(td.seconds * 1000 + td.microseconds / 1000) << 4, td.days)))

    def trigger_tpdo(self, tpdo): # Event-driven TPDO
        tpdo_cp = self.od.get(ODI_TPDO1_COMMUNICATION_PARAMETER + tpdo - 1)
        if tpdo_cp is not None:
            tpdo_cp_id = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_ID)
            if tpdo_cp_id is not None and (tpdo_cp_id >> TPDO_COMM_PARAM_ID_VALID_BITNUM) & 1 == 0:
                tpdo_cp_type = tpdo_cp.get(ODSI_TPDO_COMM_PARAM_TYPE)
                if tpdo_cp_type is not None and (tpdo_cp_type == 0xFE or tpdo_cp_type == 0xFF):
                    self._send_pdo(tpdo)
                else:
                    self._tpdo_triggers[tpdo] = True # Defer until SYNC event