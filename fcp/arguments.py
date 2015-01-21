'''
Common argparse arguments for each script.
'''

import argparse

import fcp.node

def timeout_type(time_str):
    '''
    Argparse argument type for timeouts.
    '''
    try:
        return fcp.node.parseTime(time_str)
    except:
        raise argparse.ArgumentTypeError("Invalid timeout: %s" % time_str)

def add_default_arguments(parser):
    '''
    Add the default arguments to an argparse parser.
    '''
    parser.add_argument('--version', '-V', action='version',
                        version="%(prog)s " + fcp.node.fcpVersion)
    parser.add_argument('--fcphost', '-H', default=fcp.node.defaultFCPHost,
                        help="""Connect to FCP service at host <FCPHOST>.
                             (You may also use the environment variable
                              FCP_HOST.)
                             """)
    parser.add_argument('--fcpport', '-P', default=fcp.node.defaultFCPPort,
                        type=int,
                        help="""Connect to FCP service at port <FCPPORT>.
                             (You may also use the environment variable
                              FCP_PORT.)
                             """)
    parser.add_argument('--timeout', '-t', default=fcp.node.ONE_YEAR,
                        type=timeout_type,
                        help="""Set the timeout, in seconds, for completion.
                             Default one year.
                             """)
