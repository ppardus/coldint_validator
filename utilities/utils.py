import functools
import multiprocessing
import os
import time
import resource
import traceback
from typing import Any, List, Optional, Tuple
import bittensor as bt
import constants

# Needed to get proper logging between child and parent process
from bittensor.btlogging.defines import BITTENSOR_LOGGER_NAME
import logging as stdlogging
from logging.handlers import QueueHandler,QueueListener
import atexit

from model.data import ModelId, ModelMetadata


def assert_registered(wallet: bt.wallet, metagraph: bt.metagraph) -> int:
    """Asserts the wallet is a registered miner and returns the miner's UID.

    Raises:
        ValueError: If the wallet is not registered.
    """
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        raise ValueError(
            f"You are not registered. \nUse: \n`btcli s register --netuid {metagraph.netuid}` to register via burn \n or btcli s pow_register --netuid {metagraph.netuid} to register with a proof of work"
        )
    uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    bt.logging.success(
        f"You are registered with address: {wallet.hotkey.ss58_address} and uid: {uid}"
    )

    return uid


def validate_hf_repo_id(repo_id: str) -> Tuple[str, str]:
    """Verifies a Hugging Face repo id is valid and returns it split into namespace and name.

    Raises:
        ValueError: If the repo id is invalid.
    """

    if not repo_id:
        raise ValueError("Hugging Face repo id cannot be empty.")

    if not 3 < len(repo_id) <= ModelId.MAX_REPO_ID_LENGTH:
        raise ValueError(
            f"Hugging Face repo id must be between 3 and {ModelId.MAX_REPO_ID_LENGTH} characters. Got={repo_id}"
        )

    parts = repo_id.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"Hugging Face repo id must be in the format <org or user name>/<repo_name>. Got={repo_id}"
        )

    return parts[0], parts[1]


def get_hf_url(model_metadata: ModelMetadata) -> str:
    """Returns the URL to the Hugging Face repo for the provided model metadata."""
    return f"https://huggingface.co/{model_metadata.id.namespace}/{model_metadata.id.name}/tree/{model_metadata.id.commit}"


def _wrapped_func(func: functools.partial, log_queue: multiprocessing.Queue, queue: multiprocessing.Queue):
    resource.setrlimit(resource.RLIMIT_NOFILE, (65000, 65000))
    try:
        if log_queue is not None:
            # This feature is not (yet) available on bittensor
            #bt.logging.set_queue(log_queue)
            # Hack in a queue handler, avoid private variables
            try:
                logger = stdlogging.getLogger(BITTENSOR_LOGGER_NAME)
                while len(logger.handlers):
                    logger.removeHandler(logger.handlers[0])
                queue_handler = QueueHandler(log_queue)
                logger.addHandler(queue_handler)
                queue_handler.setLevel(stdlogging.INFO)
                logger.setLevel(stdlogging.INFO)
            except Exception as e:
                print(f'Non-fatal: exception trying to implement proper logging: {e}')
        result = func()
        queue.put((result,))
    except (Exception, BaseException) as e:
        # Catch exceptions here to add them to the queue.
        stack_trace = traceback.format_exc()
        queue.put((e,stack_trace))


def run_in_subprocess(func: functools.partial, ttl: int, mode="fork", expected_errors={}) -> Any:
    """Runs the provided function on a subprocess with 'ttl' seconds to complete.

    Args:
        func (functools.partial): Function to be run.
        ttl (int): How long to try for in seconds.

    Returns:
        Any: The value returned by 'func'
    """
    ctx = multiprocessing.get_context(mode)
    queue = ctx.Queue()
    # When forking, the log queue survives, but when spawning a process, logging
    # is re-initialized and the queue has to be set manually.
    log_queue = None
    listener = None
    if mode == 'spawn':
        # This can only work if bittensor would use mp.Manager().Queue() to
        # create the right kind of queue.
        #log_queue = bt.logging.get_queue()
        # Roll our own, re-log to existing handlers, avoid bt.logging private variables.
        try:
            logger = stdlogging.getLogger(BITTENSOR_LOGGER_NAME)
            log_queue = multiprocessing.Manager().Queue()
            listener = QueueListener(log_queue, *logger.handlers, respect_handler_level=True)
            listener.start()
            atexit.register(listener.stop)
        except Exception as e:
            bt.logging.warning(f'Non-fatal: failed to implement proper logging for child process: {e}')
    process = ctx.Process(target=_wrapped_func, args=[func, log_queue, queue])

    process.start()

    process.join(timeout=ttl)

    if listener is not None:
        try:
            listener.stop()
            atexit.unregister(listener.stop)
        except:
            pass

    if process.is_alive():
        process.terminate()
        process.join()
        raise TimeoutError(f"Failed to {func.func.__name__} after {ttl} seconds")

    # Raises an error if the queue is empty. This is fine. It means our subprocess timed out.
    try:
        result = queue.get(block=False)
    except Exception as e:
        raise Exception(f"Failed to get result from subprocess {func.func.__name__}(*args={func.args},**kwargs={func.keywords}): {e}") from None

    if not isinstance(result,tuple):
        raise Exception(f"Unexpected result from subprocess, type {type(result)}: {result}")

    # If we put an exception on the queue then raise instead of returning.
    if isinstance(result[0], Exception):
        if type(result[0]).__name__ not in expected_errors:
            bt.logging.error(f"Exception in subprocess:\n{result[1]}")
        raise result[0]
    if isinstance(result[0], BaseException):
        bt.logging.error(f"BaseException in subprocess:\n{result[1]}")
        raise Exception(f"BaseException raised in subprocess: {str(result[0])}")

    return result[0]


def get_version(filepath: str) -> Optional[int]:
    """Loads a version from the provided filepath or None if the file does not exist.

    Args:
        filepath (str): Path to the version file."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            line = f.readline()
            if line:
                return int(line)
            return None
    return None


def save_version(filepath: str, version: int):
    """Saves a version to the provided filepath."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(str(version))


def move_file_if_exists(src: str, dst: str) -> bool:
    """Moves a file from src to dst if it exists.

    Returns:
        bool: True if the file was moved, False otherwise.
    """
    if os.path.exists(src) and not os.path.exists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return True
    return False


def list_top_miners(metagraph: bt.metagraph) -> List[int]:
    """Returns the list of top miners, chosen based on weights set on the largest valis.

    Args:
        metagraph (bt.metagraph): Metagraph to use. Must not be lite.
    """

    top_miners = set()

    # Find the top 10 valis by stake.
    valis_by_stake = get_top_valis(metagraph, 10)

    # For each, find the miner that has more than 50% of the weights.
    for uid in valis_by_stake:
        if uid >= len(metagraph.weights):
            bt.logging.warning(f"Vali UID {uid} not in metagraph.weights")
            continue

        weights = [(uid, w) for uid, w in enumerate(metagraph.weights[uid]) if w > 0]
        total_weight = sum(weight for _, weight in weights)

        # Only look for miners with at least half the weight from this vali
        threshold = constants.TOP_MINER_FRACTION * total_weight
        for uid, weight in weights:
            if weight > threshold:
                top_miners.add(uid)

    return list(top_miners)


def get_top_valis(metagraph: bt.metagraph, n: int) -> List[int]:
    """Returns the N top validators, ordered by stake descending.

    Returns:
      List[int]: Ordered list of UIDs of the top N validators, or all validators if N is greater than the number of validators.
    """
    valis = []
    for uid, stake in enumerate(metagraph.S):
        # Use vPermit to check for validators rather than vTrust because we'd rather
        # cast a wide net in the case that vTrust is 0 due to an unhealthy state of the
        # subnet.
        if metagraph.validator_permit[uid]:
            valis.append((stake, uid))

    return [uid for _, uid in sorted(valis, reverse=True)[:n]]


def run_with_retry(func, max_retries=3, delay_seconds=1, single_try_timeout=30):
    """
    Retry a function with constant backoff.

    Parameters:
    - func: The function to be retried.
    - max_retries: Maximum number of retry attempts (default is 3).
    - delay_seconds: Initial delay between retries in seconds (default is 1).

    Returns:
    - The result of the successful function execution.
    - Raises the exception from the last attempt if all attempts fail.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries:
                # If it's the last attempt, raise the exception
                raise e
            # Wait before the next retry.
            time.sleep(delay_seconds)
    raise Exception("Unexpected state: Ran with retry but didn't hit a terminal state")
