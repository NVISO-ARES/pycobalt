"""
For communication with Cobalt Strike
"""

# TODO better argument checking on aggressor functions

import json
import re
import sys
import traceback

import os
#sys.path.insert(0, os.path.realpath(os.path.dirname(__file__)) + '/..')

import pycobalt.utils as utils
import pycobalt.callbacks as callbacks
import pycobalt.serialization as serialization

_in_pipe = None
_out_pipe = None

_debug_on = False

def _init_pipes():
    """
    Configure input and output pipes. At the moment we use stdin/out/err. This
    just makes configuring scripts a bit easier. It would be pretty easy to use
    a couple of fifos but then we have to pass them to the Python script.
    Passing them on argv seems kind of dirty too.
    """

    global _in_pipe
    global _out_pipe

    _in_pipe = sys.stdin
    _out_pipe = sys.stdout

def enable_debug():
    """
    Enable debug messages on the Python side

    To enable the Aggressor debug messages run `python-debug` in the Script
    Console or set `$pycobalt_debug_on = true` in your Aggressor script.
    """

    global _debug_on
    _debug_on = True
    debug('enabled debug')

def disable_debug():
    """
    Disable debug messages
    """

    global _debug_on
    debug('disabling debug')
    _debug_on = False

def debug(line):
    """
    Write script console debug message

    :param line: Line to write
    """

    global _debug_on
    if _debug_on:
        write('debug', str(line))

def handle_exception_softly(exc):
    """
    Print an exception to the script console

    :param exc: Exception to print
    """

    try:
        raise exc
    except Exception as e:
        error('Exception: {}\n'.format(str(e)))
        error('Traceback: {}'.format(traceback.format_exc()))

def write(message_type, message=''):
    """
    Write a message to Cobalt Strike. Message can be anything serializable by
    `serialization.py`. This includes primitives, bytes, lists, dicts, tuples,
    and callbacks (automatically registered).

    :param message_type: Type/label of message
    :param message: Message contents
    """

    global _out_pipe
    wrapper = {
        'name': message_type,
        'message': message,
    }
    serialized = serialization.serialized(wrapper)
    _out_pipe.write(serialized + "\n")
    _out_pipe.flush()

def handle_message(name, message):
    """
    Handle a received message according to its name

    :param name: Name/type/label of message
    :param message: Message body
    """


    #debug('handling message of type {}: {}'.format(name, message))
    if name == 'callback':
        # dispatch callback
        callback_name = message['name']
        callback_args = message['args'] if 'args' in message else []

        if 'sync' in message and message['sync'] and 'id' in message:
            return_message = callbacks.call(callback_name, callback_args, return_id=message['id'])
            write('return', return_message)
        else:
            return_message = callbacks.call(callback_name, callback_args)

    elif name == 'eval':
        # eval python code
        eval(message)
    elif name == 'debug':
        # set debug mode
        if message is True:
            enable_debug()
        else:
            disable_debug()
    elif name == 'stop':
        # stop script
        stop()
    elif not name:
        error('Error reading pipe: {}'.format(str(message)))
        handle_exception_softly(message)
    else:
        error('Received unhandled or out-of-order message type: {} {}'.format(name, str(message)))

def parse_line(line):
    """
    Parse a serialized input line for passing to `engine.handle_message`.

    :param line: Line to parse. Should look like {'name':<name>, 'message':<message>}
    :return: Tuple containing 'name' and 'message'
    """

    try:
        # remove shitty unicode
        line = line.encode('utf-8', 'ignore').decode()
        line = line.strip()
        wrapper = json.loads(line, strict=False)
        name = wrapper['name']
        if 'message' in wrapper:
            message = wrapper['message']
        else:
            message = None

        return name, message
    except Exception as e:
        return None, e

_has_forked = False
def fork():
    """
    Tell Cobalt Strike to fork into a new thread.

    Menu trees have to be registered before we fork into a new thread so this
    is called in `engine.loop()` after the registration is finished.
    """

    global _has_forked

    if _has_forked:
        raise RuntimeError('Tried to fork Cobalt Strike twice')

    write('fork')

def read_pipe():
    """
    read_pipe a message line

    :return: Tuple containing message name and contents (as returned by
             `parse_line`).
    """

    global _in_pipe
    return parse_line(next(_in_pipe))

def read_pipe_iter():
    """
    read_pipe message lines

    :return: Iterator with an item for each read_pipe/parsed line. Each item is the
             same as the return value of `engine.read_pipe()`.
    """

    global _in_pipe
    for line in _in_pipe:
        yield parse_line(line)

def loop(fork_first=True):
    """
    Loop forever, handling messages. Does not return until the pipe closes.
    Exceptions are printed to the script console.

    :param fork_first: Whether to call `fork()` first.
    """

    global _has_forked

    if fork_first and not _has_forked:
        fork()

    for name, message in read_pipe_iter():
        try:
            if name:
                handle_message(name, message)
            else:
                error('Received invalid message: {}'.format(message))
        except Exception as e:
            handle_exception_softly(e)

def stop():
    """
    Stop the script (just exits the process)
    """

    sys.exit()

def call(name, args=None, silent=False, fork=None, sync=True):
    """
    Call a sleep/aggressor function. You should use the `aggressor.py` helpers
    where possible.

    :param name: Name of function to call
    :param args: Arguments to pass to function
    :param silent: Don't print tasking information (! operation) (only works
                   for some functions)
    :param fork: Call in its own thread
    :param sync: Wait for return value
    :return: Return value of function if `sync` is True
    """

    if args is None:
        # no arguments
        args = []

    # serialize and register function callbacks if needed
    if fork is None:
        if callbacks.has_callback(args):
            # when there's a callback involved we usually have to fork because the
            # main script thread is busy reading from the script.
            #debug("forcing fork for call to: {}".format(name))
            fork = True
        else:
            fork = False

    message = {
        'name': name,
        'args': args,
        'silent': silent,
        'fork': fork,
        'sync': sync,
    }
    write('call', message)

    if sync:
        # read_pipe and handle messages until we get our return value
        for name, message in read_pipe_iter():
            if name == 'return':
                # got it
                return message
            else:
                try:
                    handle_message(name, message)
                except Exception as e:
                    handle_exception_softly(e)

def eval(code):
    """
    Eval aggressor code. Does not provide a return value.

    :param code: Code to eval
    """

    write('eval', code)

def menu(menu_items):
    """
    Register a Cobalt Strike menu tree

    :param menu_items: Menu tree as returned by the `gui.py` helpers.
    """

    global _has_forked

    if _has_forked:
        raise RuntimeError('Tried to register a menu after forking. This crashes Cobalt Strike')

    write('menu', menu_items)

def error(line):
    """
    Write error notice

    :param line: Line to write
    """

    write('error', str(line))

def message(line):
    """
    Write script console message. The Aggressor side will add a prefix to your
    message. To print raw messages use `aggressor.print` and
    `aggressor.println`.

    :param line: Line to write
    """

    write('message', str(line))

def delete(handle):
    """
    Delete an object with its serialized handle. This just removed the global
    reference. The object will stick around if it's referenced elsewhere.

    :param handle: Handle of object to delete
    """

    write('delete', handle)

_init_pipes()
