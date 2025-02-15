"""This module provides a base class for transports that provide
asynchronous (non-blocking) transmit and receive functionality.
"""


from thespian.system.transport import (TransmitOnly, SendStatus,
                                       Thespian__UpdateWork)
from thespian.system.utilis import thesplog, partition, getenvdef
from thespian.system.timing import ExpirationTimer
import logging
from thespian.system.addressManager import CannotPickleAddress
from collections import deque
import threading
from contextlib import contextmanager
import time


if hasattr(threading, 'main_thread'):
    # python 3.4 or later
    is_main_thread = lambda: threading.main_thread() == threading.current_thread()
else:
    if hasattr(threading, 'name'):
        is_main_thread = lambda: 'MainThread' in threading.current_thread().name
    else:
        is_main_thread = lambda: 'MainThread' in threading.current_thread().getName()


# Transmits are passed along until there are MAX_PENDING_TRANSMITS, at
# which point they are queued internally.  If the number of internally
# queue transmits exceeds MAX_QUEUED_TRANSMITS then the transport is
# put into transmit-only mode (attempting to drain all current work
# before new work is accepted) until the transmit queue depth drops
# back below QUEUE_TRANSMIT_UNBLOCK_THRESHOLD.  If the number of
# queued transmits exceeds the DROP_TRANSMITS_LEVEL then additional
# transmits are immediately failed instead of being queued.

MAX_PENDING_TRANSMITS = getenvdef('THESPIAN_MAX_PENDING_TRANSMITS', int, 20)
MAX_QUEUED_TRANSMITS = getenvdef('THESPIAN_MAX_QUEUED_TRANSMITS', int, 950)
QUEUE_TRANSMIT_UNBLOCK_THRESHOLD = getenvdef('THESPIAN_QUEUED_TRANSMIT_UNBLOCK_THRESHOLD', int, 780)
DROP_TRANSMITS_LEVEL = getenvdef('THESPIAN_DROP_TRANSMITS_LEVEL', int, MAX_QUEUED_TRANSMITS + 100)


@contextmanager
def exclusive_processing(transport):
    while not transport._exclusively_processing():
        time.sleep(0.000001)
    yield
    transport._not_processing()


class asyncTransportBase(object):
    """This class should be used as a base-class for Transports where the
       transmit operation occurs asynchronously.  The send operation
       will reject TransmitIntent objects until they are fully
       serializeable, and will then submit the TransmitIntent to the
       actual Transport for sending.

       This module provides queue management for transmits to ensure
       that only a limited number of transmits are active from this
       Actor at any one time.  Note that the system level
       functionality is responsible for ensuring that only one
       TransmitIntent *PER TARGET* is submitted to this module at any
       one time, but this module ensures that the number of
       TransmitIntents *FOR ALL TARGETS* does not exceed a maximum
       threshold.
    """

    # Expects from subclass:
    #   self.serializer         - serializer callable that returns serialized form
    #                             of intent that should be sent (stored in .serMsg)
    #   self._scheduleTransmitActual -- called to do the actual transmit (with .serMsg set)

    def __init__(self, *args, **kw):
        super(asyncTransportBase, self).__init__(*args, **kw)
        self._aTB_numPendingTransmits = 0  # counts recursion and in-progress
        self._aTB_lock = threading.Lock()  # protects the following:
        self._aTB_processing = False       # limits to a single operation
        self._aTB_sending = False          # transmit is being performed
        self._aTB_queuedPendingTransmits = deque()
        self._aTB_rx_pause_enabled = True
        self._aTB_interrupted = False


    def setAddressManager(self, addrManager):
        self._addressMgr = addrManager


    def enableRXPauseFlowControl(self, enable=True):
        self._aTB_rx_pause_enabled = enable


    def _updateStatusResponse(self, resp):
        """Called to update a Thespian_SystemStatus or Thespian_ActorStatus
           with common information
        """
        with self._aTB_lock:
            for each in self._aTB_queuedPendingTransmits:
                resp.addPendingMessage(self.myAddress,
                                       each.targetAddr,
                                       each.message)


    def _canSendNow(self):
        return (MAX_PENDING_TRANSMITS > self._aTB_numPendingTransmits)

    def _async_txdone(self, _TXresult, _TXIntent):
        self._aTB_numPendingTransmits -= 1

        # If in the context of an initiated transmit, do not process
        # timeouts or do more scheduling because that could recurse
        # indefinitely.  In addition, ensure that this is not part of
        # a callback chain that has looped back around here, which
        # also represents recursion.  All those entry points will
        # re-check for additional work and initiated the work at that
        # point.
        while self._canSendNow():
            if not self._runQueued():
                break

    def _runQueued(self, has_exclusive_flag=False):
        """Perform queued transmits; returns False if there are no transmits
           or if another process is already in this critical section
           (and will therefore be perform the transmits).
        """
        v, e = self._complete_expired_intents()
        while e:
            v, e = self._complete_expired_intents()
        # If something is queued, submit it to the lower level for transmission
        # 1. Sync with the lower level, since this will be modifying lower-level objects
        while True:
            nextTransmit = None
            with self._aTB_lock:
                if has_exclusive_flag or not self._aTB_processing:
                    # 2. If another process is in the sending critical
                    # section, defer to it
                    if self._aTB_sending:
                        return False
                    # Nothing to send by this point, return
                    if not self._aTB_queuedPendingTransmits:
                        return False
                    self._aTB_processing = True
                    self._aTB_sending = True
                    nextTransmit = self._aTB_queuedPendingTransmits.popleft()
            try:
                if nextTransmit:
                    self._submitTransmit(nextTransmit)
                    return True
                return False
            finally:
                self._aTB_sending = False
                self._aTB_processing = False
            time.sleep(0.00001)


    def scheduleTransmit(self, addressManager, transmitIntent, has_exclusive_flag=False):

        """Requests that a transmit be performed.  The message and target
           address must be fully valid at this point; any local
           addresses should throw a CannotPickleAddress exception and
           the caller is responsible for retrying later when those
           addresses are available.

           If addressManager is None then the intent address is
           assumed to be valid but it cannot be updated if it is a
           local address or a dead address.  A value of None is
           normally only used at Admin or Actor startup time when
           confirming the established connection back to the parent,
           at which time the target address should always be valid.

           Any transmit attempts from a thread other than the main
           thread are queued; calls to the underlying transmit layer
           are done only from the context of the main thread.
        """

        if addressManager:
            # Verify the target address is useable
            targetAddr, txmsg = addressManager.prepMessageSend(
                transmitIntent.targetAddr,
                transmitIntent.message)
            try:
                isDead = txmsg == SendStatus.DeadTarget
            except Exception:
                # txmsg may have an __eq__ that caused an exception
                isDead = False
            if isDead:
                # Address Manager has indicated that these messages
                # should never be attempted because the target is
                # dead.  This is *only* for special messages like
                # DeadEnvelope and ChildActorExited which would
                # endlessly recurse or bounce back and forth.  This
                # code indicates here that the transmit was
                # "successful" to allow normal cleanup but to avoid
                # recursive error generation.
                thesplog('Faking dead target transmit result Sent for %s',
                         transmitIntent, level=logging.WARNING)
                transmitIntent.tx_done(SendStatus.Sent)
                return

            if not targetAddr:
                raise CannotPickleAddress(transmitIntent.targetAddr)

            # In case the prep made some changes...
            transmitIntent.changeTargetAddr(targetAddr)
            transmitIntent.changeMessage(txmsg)

        # Verify that the message can be serialized.  This may throw
        # an exception for local-only ActorAddresses or for attempting
        # to send other invalid elements in the message.  The invalid
        # address will cause the caller to store this intent and retry
        # it at some future point (the code up to and including this
        # serialization should be idempotent).

        transmitIntent.serMsg = self.serializer(transmitIntent)
        self._schedulePreparedIntent(transmitIntent, has_exclusive_flag=has_exclusive_flag)

    def _qtx(self, transmitIntent):
        with self._aTB_lock:
            if len(self._aTB_queuedPendingTransmits) < DROP_TRANSMITS_LEVEL:
                self._aTB_queuedPendingTransmits.append(transmitIntent)
                return True
        return False

    def _queue_tx(self, transmitIntent):
        if self._qtx(transmitIntent):
            return True
        thesplog('Dropping TX: overloaded', level=logging.WARNING)
        transmitIntent.tx_done(SendStatus.Failed)
        return False

    def _complete_expired_intents(self):
        with self._aTB_lock:
            expiredTX, validTX = partition(lambda i: i.expired(),
                                           self._aTB_queuedPendingTransmits,
                                           deque)
            self._aTB_queuedPendingTransmits = validTX
            rlen = len(validTX)
        for each in expiredTX:
            thesplog('TX intent %s timed out', each, level=logging.WARNING)
            each.tx_done(SendStatus.Failed)
        return rlen, bool(expiredTX)

    def _drain_tx_queue_if_needed(self, max_delay=None):
        v, _ = self._complete_expired_intents()
        if v >= MAX_QUEUED_TRANSMITS and self._aTB_rx_pause_enabled:
            # Try to drain our local work before accepting more
            # because it looks like we're getting really behind.  This
            # is dangerous though, because if other Actors are having
            # the same issue this can create a deadlock.
            finish_time = ExpirationTimer(max_delay if max_delay else None)
            thesplog('Entering tx-only mode to drain excessive queue'
                     ' (%s > %s, drain-to %s in %s)',
                     v, MAX_QUEUED_TRANSMITS,
                     QUEUE_TRANSMIT_UNBLOCK_THRESHOLD, finish_time,
                     level=logging.WARNING)
            while v > QUEUE_TRANSMIT_UNBLOCK_THRESHOLD:
                with finish_time as rem_time:
                    if rem_time.expired():
                        break
                    if 0 == self.run(TransmitOnly, rem_time.remaining()):
                        thesplog('Exiting tx-only mode because no transport work available.')
                        # This may happend because the lower-level
                        # subtransport layer has nothing left to send,
                        # so it has to return to allow this layer to
                        # queue more transmits.
                        break
                    v, _ = self._complete_expired_intents()
            thesplog('Exited tx-only mode after draining excessive queue (%s)',
                     len(self._aTB_queuedPendingTransmits),
                     level=logging.WARNING)

    def _exclusively_processing(self):
        "Protects critical sections by only allowing a single thread entry"
        with self._aTB_lock:
            if self._aTB_processing:
                return False  # Another thread is processing, not exclusive
            self._aTB_processing = True
            return True  # This thread exclusively holds the processing mutex

    def _not_processing(self):
        "Exit from critical section"
        self._aTB_processing = False

    def _schedulePreparedIntent(self, transmitIntent, has_exclusive_flag=False):
        # If there's nothing to send, that's implicit success
        if not transmitIntent.serMsg:
            transmitIntent.tx_done(SendStatus.Sent)
            return

        if isinstance(transmitIntent.message, Thespian__UpdateWork):
            # The UpdateWork should not actually be transmitted, but
            # it *should* cause the main thread to be interrupted if
            # it is in a blocking wait for work to do.
            transmitIntent.tx_done(SendStatus.Sent)
            self._aTB_interrupted = False
        else:
            if not self._queue_tx(transmitIntent):
                # TX overflow, intent discarded, no further work needed here
                return

        if not self._canSendNow():
            drainer = False
            if has_exclusive_flag or self._exclusively_processing():
                if not self._aTB_sending:
                    self._aTB_sending = True
                    drainer = True
                self._not_processing()
            if drainer:
                try:
                    self._drain_tx_queue_if_needed(transmitIntent.delay())
                finally:
                    self._aTB_sending = False
            else:
                time.sleep(0.1)  # slow down threads not performing draining

        while self._canSendNow():
            if not self._runQueued(has_exclusive_flag=has_exclusive_flag):
                # Before exiting, ensure that if the main thread
                # is waiting for input on select() that it is
                # awakened in case it needs to monitor new
                # transmit sockets.
                if not is_main_thread() and not self._aTB_interrupted:
                    self._aTB_interrupted = True
                    self.interrupt_wait()
                break


    def _submitTransmit(self, transmitIntent, has_exclusive_flag=False):
        self._aTB_numPendingTransmits += 1
        transmitIntent.addCallback(self._async_txdone, self._async_txdone)

        thesplog('actualTransmit of %s', transmitIntent.identify(),
                 level=logging.DEBUG)
        self._scheduleTransmitActual(transmitIntent, has_exclusive_flag=has_exclusive_flag)

    def deadAddress(self, addressManager, childAddr):
        # Go through pending transmits and update any to this child to
        # a dead letter delivery
        with self._aTB_lock:
            for each in self._aTB_queuedPendingTransmits:
                if each.targetAddr == childAddr:
                    newtgt, newmsg = addressManager.prepMessageSend(
                        each.targetAddr, each.message)
                    each.changeTargetAddr(newtgt)
                    # n.b. prepMessageSend might return
                    # SendStatus.DeadTarget for newmsg; when this is later
                    # attempted, that will be handled normally and the
                    # transmit will be completed as "Sent"
                    each.changeMessage(newmsg)
