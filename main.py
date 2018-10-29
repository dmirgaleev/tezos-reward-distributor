import argparse
import csv
import os
import queue
import subprocess
import threading
import time
from enum import Enum

from BussinessConfiguration import BAKING_ADDRESS, supporters_set, founders_map, owners_map, specials_map, STANDARD_FEE
from ClientConfiguration import COMM_TRANSFER
from NetworkConfiguration import network_config_map
from PaymentCalculator import PaymentCalculator
from ProcessLifeCycle import ProcessLifeCycle
from ServiceFeeCalculator import ServiceFeeCalculator
from TzScanBlockApi import TzScanBlockApi
from TzScanRewardApi import TzScanRewardApi
from TzScanRewardCalculator import TzScanRewardCalculator
from logconfig import main_logger

NB_CONSUMERS = 1
BUF_SIZE = 50
payments_queue = queue.Queue(BUF_SIZE)
logger = main_logger


class RunMode(Enum):
    FOREVER = 1
    PENDING = 2
    ONETIME = 3


EXIT_PAYMENT_TYPE = "exit"
lifeCycle = ProcessLifeCycle()


class ProducerThread(threading.Thread):
    def __init__(self, name, initial_payment_cycle, network_config, payments_dir, reports_dir, run_mode,
                 service_fee_calc, owners_map, founders_map, baking_address):
        super(ProducerThread, self).__init__()
        self.baking_address = baking_address
        self.owners_map = owners_map
        self.founders_map = founders_map
        self.name = name
        self.block_api = TzScanBlockApi(network_config)
        self.fee_calc = service_fee_calc
        self.initial_payment_cycle = initial_payment_cycle
        self.nw_config = network_config
        self.payments_dir = payments_dir
        self.reports_dir = reports_dir
        self.run_mode = run_mode

        logger.debug('Producer started')

    def run(self):
        current_cycle = self.block_api.get_current_cycle()

        payment_cycle = self.initial_payment_cycle

        # if non-positive initial_payment_cycle, set initial_payment_cycle to 'current cycle - abs(initial_cycle) - (NB_FREEZE_CYCLE+1)'
        if self.initial_payment_cycle <= 0:
            payment_cycle = current_cycle - abs(self.initial_payment_cycle) - (self.nw_config['NB_FREEZE_CYCLE'] + 1)

        while lifeCycle.is_running():

            # take a breath
            time.sleep(10)

            current_level = self.block_api.get_current_level()
            current_cycle = self.block_api.level_to_cycle(current_level)

            if os.path.isdir(payment_dir_c(self.payments_dir, payment_cycle)):
                logger.warn("Payment directory for cycle {} is present. No payment will be run for the cycle".format(
                    payment_cycle))
                payment_cycle = payment_cycle + 1
                continue

            # create reports dir
            if self.reports_dir and not os.path.exists(self.reports_dir):
                os.makedirs(self.reports_dir)

            # payments should not pass beyond last released reward cycle
            if payment_cycle <= current_cycle - (self.nw_config['NB_FREEZE_CYCLE'] + 1):
                if not payments_queue.full():
                    try:

                        logger.info("Payment cycle is " + str(payment_cycle))

                        reward_api = TzScanRewardApi(self.nw_config, self.baking_address)
                        reward_data = reward_api.get_rewards_for_cycle_map(payment_cycle)
                        reward_calc = TzScanRewardCalculator(self.founders_map, reward_data)
                        rewards = reward_calc.calculate()
                        total_rewards = reward_calc.get_total_rewards()

                        if total_rewards == 0:
                            logger.info("Total rewards is zero skipping payment")
                            payment_cycle = payment_cycle + 1
                            continue
                        else:
                            logger.info("Total rewards=" + str(total_rewards))

                        payment_calc = PaymentCalculator(self.founders_map, self.owners_map, rewards, total_rewards,
                                                         self.fee_calc, payment_cycle)
                        payments = payment_calc.calculate()

                        report_file_path = self.reports_dir + '/' + str(payment_cycle) + '.csv'
                        with open(report_file_path, 'w', newline='') as csvfile:
                            csvwriter = csv.writer(csvfile, delimiter='\t', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                            # write headers and total rewards
                            csvwriter.writerow(["address", "type", "ratio", "reward", "fee_rate", "payment", "fee"])
                            csvwriter.writerow([self.baking_address, "B", 1.0, total_rewards, 0, total_rewards, 0])

                            for payment_item in payments:
                                address = payment_item["address"]
                                payment = payment_item["payment"]
                                fee = payment_item["fee"]
                                type = payment_item["type"]
                                ratio = payment_item["ratio"]
                                reward = payment_item["reward"]
                                fee_rate = self.fee_calc.calculate(address)

                                # write row to csv file
                                csvwriter.writerow([address, type, "{0:f}".format(ratio), "{0:f}".format(reward),
                                                    "{0:f}".format(fee_rate), "{0:f}".format(payment),
                                                    "{0:f}".format(fee)])

                                pymt_log = payment_file_name(self.payments_dir, str(payment_cycle), address, type)

                                if os.path.isfile(pymt_log):
                                    logger.warning(
                                        "Reward not created for cycle %s address %s amount %f tz %s: Reason payment log already present",
                                        payment_cycle, address, payment, type)
                                else:
                                    payments_queue.put(payment_item)
                                    logger.info("Reward created for cycle %s address %s amount %f fee %f tz type %s",
                                                payment_cycle, address, payment, fee, type)

                            # processing of cycle is done
                            logger.info("Reward creation done for cycle %s", payment_cycle)
                            payment_cycle = payment_cycle + 1

                        # single run is done. Do not continue.
                        if self.run_mode == RunMode.ONETIME:
                            logger.info("Run mode ONETIME satisfied. Killing the thread ...")
                            payments_queue.put(self.create_exit_payment())
                            return
                    except Exception as e:
                        logger.error("Error at reward calculation", e)

                # end of queue size check
                else:
                    logger.debug("Wait a few minutes, queue is full")
                    # wait a few minutes to let payments done
                    time.sleep(60 * 3)
            # end of payment cycle check
            else:
                # pending payments done. Do not wait any more.
                if self.run_mode == RunMode.PENDING:
                    logger.info("Run mode PENDING satisfied. Killing the thread ...")
                    payments_queue.put(self.create_exit_payment())
                    break

                # calculate number of blocks until end of current cycle
                nb_blocks_remaining = (current_cycle + 1) * self.nw_config['BLOCKS_PER_CYCLE'] - current_level

                logger.debug("Wait until next cycle, for {} blocks".format(nb_blocks_remaining))

                # wait until current cycle ends
                for x in range(nb_blocks_remaining):
                    time.sleep(self.nw_config['BLOCK_TIME_IN_SEC'])

                    # if shutting down, exit
                    if not lifeCycle.is_running():
                        payments_queue.put(self.create_exit_payment())
                        break

        # end of endless loop
        logger.info("Producer returning ...")
        return

    def create_exit_payment(self):
        return {'payment': 0, 'fee': 0, 'address': 0, 'cycle': 0, 'type': EXIT_PAYMENT_TYPE, 'ratio': 0, 'reward': 0}


class ConsumerThread(threading.Thread):
    def __init__(self, name, payments_dir, key_name, transfer_command):
        super(ConsumerThread, self).__init__()

        self.name = name
        self.payments_dir = payments_dir
        self.key_name = key_name
        self.transfer_command = transfer_command

        logger.debug('Consumer "%s" created', self.name)

        return

    def run(self):
        while lifeCycle.is_running():
            try:
                # wait until a reward is present
                payment_item = payments_queue.get(True)

                pymnt_addr = payment_item["address"]
                pymnt_amnt = payment_item["payment"]
                pymnt_cycle = payment_item["cycle"]
                type = payment_item["type"]

                if type == EXIT_PAYMENT_TYPE:
                    logger.debug("Exit signal received. Killing the thread...")
                    break

                cmd = self.transfer_command.format(pymnt_amnt, self.key_name, pymnt_addr)

                logger.debug("Reward payment attempt for cycle %s address %s amount %f tz type %s", pymnt_cycle,
                             pymnt_addr,
                             pymnt_amnt, type)

                logger.debug("Reward payment command '{}'".format(cmd))

                # execute client
                process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
                process.wait()

                if process.returncode == 0:
                    pymt_log = payment_file_name(self.payments_dir, str(pymnt_cycle), pymnt_addr, type)

                    # check and create required directories
                    if not os.path.exists(os.path.dirname(pymt_log)):
                        os.makedirs(os.path.dirname(pymt_log))

                    # create empty payment log file
                    with open(pymt_log, 'w') as f:
                        f.write('')

                    logger.info("Reward paid for cycle %s address %s amount %f tz", pymnt_cycle, pymnt_addr, pymnt_amnt)
                else:
                    logger.warning("Reward NOT paid for cycle %s address %s amount %f tz: Reason client failed!",
                                   pymnt_cycle, pymnt_addr, pymnt_amnt)
            except Exception as e:
                logger.error("Error at reward payment", e)

        logger.info("Consumer returning ...")

        return


def payment_file_name(pymnt_dir, pymnt_cycle, pymnt_addr, pymnt_type):
    return payment_dir_c(pymnt_dir, pymnt_cycle) + "/" + pymnt_addr + '_' + pymnt_type + '.txt'


def payment_dir_c(pymnt_dir, pymnt_cycle):
    return pymnt_dir + "/" + str(pymnt_cycle)


# all shares in the map must sum upto 1
def validate_map_share_sum(map, map_name):
    if sum(map.values()) != 1:
        raise Exception("Map '{}' shares does not sum up to 1!".format(map_name))


def main(args):
    network_config = network_config_map[args.network]
    key = args.key
    payments_dir = os.path.expanduser(args.payments_dir)
    reports_dir = os.path.expanduser(args.reports_dir)
    run_mode = RunMode(args.run_mode)

    validate_map_share_sum(founders_map, "founders map")
    validate_map_share_sum(owners_map, "owners map")

    # if in dry run mode, do not create consumers
    # create reports in dry directory
    if args.dry_run:
        global NB_CONSUMERS
        NB_CONSUMERS = 0
        reports_dir = "./dry"

    lifeCycle.start(not args.dry_run)

    full_supporters_set = supporters_set | set(founders_map.keys()) | set(owners_map.keys())

    service_fee_calc = ServiceFeeCalculator(supporters_set=full_supporters_set, specials_map=specials_map,
                                            standard_fee=STANDARD_FEE)

    if args.initial_cycle is None:
        recent = None
        if os.path.isdir(payments_dir):
            files = sorted(os.listdir(payments_dir), key=lambda x: int(x))
            recent = files[-1] if len(files) > 0 else None
        # if payment logs exists set initial cycle to following cycle
        # if payment logs does not exists, set initial cycle to 0, so that payment starts from last released rewards
        args.initial_cycle = 0 if recent is None else int(recent) + 1

        logger.info("initial_cycle set to {}".format(args.initial_cycle))

    p = ProducerThread(name='producer', initial_payment_cycle=args.initial_cycle, network_config=network_config,
                       payments_dir=payments_dir, reports_dir=reports_dir, run_mode=run_mode,
                       service_fee_calc=service_fee_calc, owners_map=owners_map, founders_map=founders_map,
                       baking_address=BAKING_ADDRESS)
    p.start()

    for i in range(NB_CONSUMERS):
        c = ConsumerThread(name='consumer' + str(i), payments_dir=payments_dir, key_name=key,
                           transfer_command=COMM_TRANSFER.replace("%network%", network_config['NAME'].lower()))
        time.sleep(1)
        c.start()
    try:
        while True: time.sleep(1000)
    except KeyboardInterrupt  as e:
        logger.info("Interrupted.")
        lifeCycle.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("key", help="tezos address or alias to make payments")
    parser.add_argument("-N", "--network", help="network name", choices=['ZERONET', 'ALPHANET', 'MAINNET'],
                        default='MAINNET')
    parser.add_argument("-P", "--payments_dir", help="Directory to create payment logs", default='./payments')
    parser.add_argument("-T", "--reports_dir", help="Directory to create reports", default='./reports')
    parser.add_argument("-D", "--dry_run",
                        help="Run without doing any payments. Suitable for testing. Does not require locking.",
                        action="store_true")
    parser.add_argument("-M", "--run_mode",
                        help="Waiting decision after making pending payments. 1: default option. Run forever. 2: Run all pending payments and exit. 3: Run for one cycle and exit. Suitable to use with -C option.",
                        default=1, choices=[1, 2, 3], type=int)
    parser.add_argument("-C", "--initial_cycle",
                        help="First cycle to start payment. For last released rewards, set to 0. Non-positive values are interpreted as : current cycle - abs(initial_cycle) - (NB_FREEZE_CYCLE+1). If not set application will continue from last payment made or last reward released.",
                        type=int)

    args = parser.parse_args()

    logger.info("Tezos Reward Distributer is Starting")
    logger.info("Current network is {}".format(args.network))
    logger.info("Baker addess is {}".format(BAKING_ADDRESS))
    logger.info("Keyname {}".format(args.key))
    logger.info("--------------------------------------------")
    logger.info("Author huseyinabanox@gmail.com")
    logger.info("Please leave author information")
    logger.info("--------------------------------------------")
    if args.dry_run:
        logger.info("DRY RUN MODE")
        logger.info("--------------------------------------------")
    main(args)
