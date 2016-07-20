#!/usr/bin/env python2
# encoding: utf-8

# babcom.py --- Implementation of Freenet Communication Primitives

# Copyright (C) 2016 Arne Babenhauserheide <arne_bab@web.de>
# Copyright (C) 2013 Steve Dougherty (Freemail and WoT concepts)

# Author: Arne Babenhauserheide <arne_bab@web.de>

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


"""Implementation of Freenet Commmunication Primitives"""


import sys
import os
import argparse # commandline arguments
import cmd # interactive shell
import fcp
import random
import threading # TODO: replace by futures once we have Python3
import logging
import functools
import appdirs
import smtplib
from email.mime.text import MIMEText
import imaplib
import base64

try:
    import newbase60
    numtostring = newbase60.numtosxg
except:
    numtostring = str
        

slowtests = False


# first, parse commandline arguments
def parse_args():
    """Parse commandline arguments."""
    parser = argparse.ArgumentParser(description="Implementation of Freenet Communication Primitives")
    parser.add_argument('-u', '--user', default=None, help="Identity to use (default: create new)")
    parser.add_argument('--host', default=None, help="Freenet host address (default: 127.0.0.1)")
    parser.add_argument('--port', default=None, help="Freenet FCP port (default: 9481)")
    parser.add_argument('--verbosity', default=None, help="Set verbosity (default: 3, to FCP calls: 5)")
    parser.add_argument('--transient', default=False, action="store_true", help="Do not store any data (LIMITATION: currently there remains data in the Freenet node! It is planned to get rid of that)")
    parser.add_argument('--test', default=False, action="store_true", help="Run the tests")
    parser.add_argument('--slowtests', default=False, action="store_true", help="Run slow tests, many of them with actual network operation in Freenet")
    args = parser.parse_args()
    return args


def withprogress(func):
    """Provide progress, if we are the main thread (blocking others)"""
    @functools.wraps(func)
    def fun(*args, **kwds):
        # avoid giving progress when we’re not the main thread
        if not isinstance(threading.current_thread(), threading._MainThread):
            return func(*args, **kwds)
        
        def waiting(letter):
            def w():
                sys.stderr.write(letter)
                sys.stderr.flush()
            return w
        tasks = []
        # one per second for 20 seconds
        for i in range(1, 21):
            tasks.append(threading.Timer(i, waiting(".")))
        # one per 3 seconds for 1 minute
        for i in range(1, 21):
            tasks.append(threading.Timer(20 + i*3, waiting(":")))
        # one per 10 seconds for 3.5 minutes
        for i in range(1, 22):
            tasks.append(threading.Timer(80 + i*10, waiting("#")))
        # one per 5 minutes for the rest of the hour
        for i in range(1, 12):
            tasks.append(threading.Timer(300 + i*300, waiting("!")))
        # one per hour for 6 hours
        for i in range(1, 7):
            tasks.append(threading.Timer(3600 + i*3600, waiting("?")))
        [i.start() for i in tasks]
        try:
            res = func(*args, **kwds)
        except Exception:
            raise
        finally:
            [i.cancel() for i in tasks]
            sys.stderr.write("\n")
            sys.stderr.flush()
        return res

    return fun


# then add interactive usage, since this will be a communication tool
class Babcom(cmd.Cmd):
    prompt = "--> "
    _messageprompt = "{newidentities}{messages}> "
    _emptyprompt = "--> "
    # TODO: change to "!5> " for 5 messages which can then be checked
    #       with the command read.
    username = None
    identity = None
    requestkey = None
    #: seed identity keys for initial visibility. This is currently BabcomTest. They need to be maintained: a daemon needs to actually check their CAPTCHA queue and update the trust, and a human needs to check whether what they post is spam or not.
    seedkeys = [
        # ("USK@fVzf7fg0Va7vNTZZQNKMCDGo6-FSxzF3PhthcXKRRvA,"
        #  "~JBPb2wjAfP68F1PVriphItLMzioBP9V9BnN59mYb0o,"
        #  "AQACAAE/WebOfTrust/12"),
        ("USK@FZynnK5Ngi6yTkBAZXGbdRLHVPvQbd2poW6DmZT8vbs,"
         "bcPW8yREf-66Wfh09yvx-WUt5mJkhGk5a2NFvbCUDsA,"
         "AQACAAE/WebOfTrust/1"),
    ]
    seedtrust = 100
    # iterators which either return a CAPTCHA or None.
    captchaiters = [] # one iterator per set of captcha-sources to check
    captchas = [] # retrieved captchas I could solve
    captchawatchers = [] # one iterator per set of captchasolutionkeys
    captchasolutions = [] # captcha solutions to watch for new identities
    newlydiscovered = [] # newly discovered IDs
    messages = [] # new messages the user can read
    timers = []
    transient = False # in transient operation, nothing is saved to
                      # disk. TODO: spin up a temporary freenet node
                      # in transient operation.

    def preloop(self):
        if self.username is None:
            self.username = randomname()
            print("No user given.")
            print("Generating random identity with name", self.username, "Please wait...")
        else:
            print("Retrieving identity information from Freenet using name", self.username + ". Please wait ...")

        matches = myidentity(self.username)
        print("... retrieved", len(matches), "identities matching", self.username)
        if matches[1:]:
            choice = None
            print("more than one identity with name", self.username, "please select one.")
            while choice is None:
                for i in range(len(matches)):
                    print(i+1, matches[i][0]+"@"+matches[i][1]["Identity"])
                res = input("Insert the number of the identity to use (1 to " + str(len(matches)) + "): ")
                try:
                    choice = int(res)
                except ValueError:
                    print("not a number")
                if choice < 1 or len(matches) < choice:
                    choice = None
                    print("the number is not in the range", str(i+1), "to", str(len(matches)))
            self.username = matches[choice - 1][0]
            self.identity = matches[choice - 1][1]["Identity"]
            self.requestkey = matches[choice - 1][1]["RequestURI"]
        else:
            self.username = matches[0][0]
            self.identity = matches[0][1]["Identity"]
            self.requestkey = matches[0][1]["RequestURI"]

        # load persistent state from disk (i.e. unused captcha solutions)
        self.load()
        
        print("Logged in as", self.username + "@" + self.identity)
        print("    with key", self.requestkey)
        # start watching captcha solutions
        self.watchcaptchasolutionloop()
        
        def introduce():
            # TODO: write solutions to a file on disk and re-read a
            # limited number of them on login.
            solutions = providecaptchas(self.identity)
            self.captchasolutions.extend(solutions)
            self.watchcaptchasolutions(solutions)
            self.messages.append("New CAPTCHAs uploaded successfully.")
        t = threading.Timer(0, introduce)
        t.daemon = True
        t.start()
        self.timers.append(t)
        print("Providing new CAPTCHAs, so others can make themselves visible.""")
        print()

    def postloop(self):
        """Cleanup and save state."""
        # TODO: This does not fire on quit or EOF. The functionality
        #       is for now implemented in do_quit and do_EOF.

    def postcmd(self, stop, line):
        # update message information after every command
        self.updateprompt()
        
    def save(self):
        """Save state like the CAPTCHA solutions."""
        if self.transient:
            return # not saving anything in transient mode
        
        dirs = appdirs.AppDirs("babcom", "freenet")
        # dirs.user_data_dir 
        # dirs.user_config_dir 
        # dirs.user_cache_dir
        # dirs.user_log_dir
        identity_info_dir = os.path.join(dirs.user_data_dir, self.identity)
        if not os.path.isdir(identity_info_dir):
            os.makedirs(identity_info_dir)
        with open(os.path.join(
                identity_info_dir, "unusedcaptchasolutions.txt"), "w") as f:
            f.write("\n".join(self.captchasolutions))
        logging.debug("saved {}".format(self.captchasolutions))
        
    def load(self):
        """Save state like the CAPTCHA solutions."""
        dirs = appdirs.AppDirs("babcom", "freenet")
        identity_info_dir = os.path.join(dirs.user_data_dir, self.identity)
        if not os.path.isdir(identity_info_dir):
            return

        with open(os.path.join(
                identity_info_dir, "unusedcaptchasolutions.txt")) as f:
            solutions = [i.strip() for i in f.readlines()]
        c = set(self.captchasolutions)
        self.captchasolutions.extend(
            [i for i in solutions if i not in c])
        logging.debug("loaded {}".format(solutions))
            
    def watchcaptchasolutions(self, solutions, maxwatchers=100):
        """Start watching the solutions of captchas, adding trust 0 as needed.

        The real work is done by watchcaptchasolutionsloop.
        """
        # avoid duplicates
        c = set(self.captchasolutions)
        s = [i for i in solutions if i not in c]
        self.captchasolutions.extend(s)
        # never watch more than maxwatchers solutions: drop old entries
        self.captchasolutions = self.captchasolutions[-maxwatchers:]
        # watch the solutions.
        self.captchawatchers.append(watchcaptchas(s))

    def updateprompt(self):
        nummsg = len(self.messages)
        numnew = len(self.newlydiscovered)
        if nummsg + numnew != 0:
            newids = (numtostring(numnew) if numnew > 0 else "!")
            newmsg = (numtostring(nummsg) if nummsg > 0 else "-")
            self.prompt = self._messageprompt.format(newidentities=newids,
                                                     messages=newmsg)
        else:
            self.prompt = self._emptyprompt

    def watchcaptchasolutionloop(self, intervalseconds=300):
        """Watch for captchasolutions in an infinite, offthread loop, adding solutions to newlydiscovered."""
        def loop():
            # resubmit all unsolved captchas if there are no captchasolutions left.
            if not self.captchawatchers and self.captchasolutions:
                self.watchcaptchasolutions(self.captchasolutions)

            for watcher in self.captchawatchers[:]:
                try:
                    res = next(watcher)
                except StopIteration:
                    self.captchawatchers.remove(watcher)
                    continue
                if res is None:
                    continue
                solution, newrequestkey = res
                # remember that the captcha has been solved: do not try again
                try:
                    self.captchasolutions.remove(solution)
                except ValueError: # already removed
                    pass
                newidentity = identityfrom(newrequestkey)
                print(newidentity)
                trustifmissing = 0
                commentifmissing = "Trust received from solving a CAPTCHA"
                trustadded = ensureavailability(newidentity, newrequestkey, self.identity,
                                                trustifmissing=trustifmissing,
                                                commentifmissing=commentifmissing)
                # now the identity is there, but it might not have needed explicit trust.
                # but captchas should give that.
                if not trustadded:
                    trust = gettrust(self.identity, newidentity)
                    if trust == "Nonexistent" or int(trust) < 0:
                        settrust(self.identity, newidentity, trustifmissing, commentifmissing)
                        trustadded = True
                if trustadded:
                    self.newlydiscovered.append(newrequestkey)
                    self.messages.append("New identity added who solved a CAPTCHA: {}".format(newidentity))
                else:
                    self.messages.append("Identity {} who solved a CAPTCHA was already known.".format(newidentity))
            
            t = threading.Timer(intervalseconds, loop)
            t.daemon = True
            t.start()
            self.timers.append(t)
            # cleanup the timers
            for t in self.timers:
                if not t.is_alive():
                    t.join()
                    self.timers.remove(t)
        loop()
        
        
    def do_intro(self, *args):
        "Introduce Babcom"
        print("""
It began in the Earth year 2016, with the founding of the first of
the Babcom systems, located deep in decentralized space. It was a
port of call for journalists, writers, hackers, activists . . . and
travelers from a hundred worlds. Could be a dangerous place – but we
accepted the risk, because Babcom 1 was societies next, best hope for
survival.
— Tribute to Babylon 5, where humanity learned to forge its own path.

Type help or help <command> to learn how to use babcom.

If the prompt changes from --> to !M>, N-> or NM>,
   you have new messages. Read them with read
""")
    # for testing: 
    # introduce USK@FpcnriKy19ztmHhg0QzTJjGwEJJ0kG7xgLiOvKXC7JE,CIpXjQej5StQRC8LUZnu3nvvh1l9UbZMinyFQyLSdMY,AQACAAE/WebOfTrust/0
    # introduce USK@0kq3fHCn12-93PSV4kk56B~beIkh-XfjennLapmmapM,9hQr66rxc9O5ptdmfhMk37h2vZGrsE6NYXcFDMGMiTw,AQACAAE/WebOfTrust/1
    # introduce USK@0kq3fHCn12-93PSV4kk56B~beIkh-XfjennLapmmapM,9hQr66rxc9O5ptdmfhMk37h2vZGrsE6NYXcFDMGMiTw,AQACAAE/WebOfTrust/1
    # introduce USK@FZynnK5Ngi6yTkBAZXGbdRLHVPvQbd2poW6DmZT8vbs,bcPW8yREf-66Wfh09yvx-WUt5mJkhGk5a2NFvbCUDsA,AQACAAE/WebOfTrust/1
    # introduce USK@B324z0kMF27IjNEVqn6oRJPJohAP2NRZDFhQngZ1GOI,DRf8JZviHLIFOYOdu42GLL2tDhVaWb6ihdNO18DkTpc,AQACAAE/WebOfTrust/0

    def do_read(self, *args):
        """Read messages."""
        if len(self.messages) + len(self.newlydiscovered) == 0:
            print("No new messages.")
        i = 1
        while self.messages:
            print("[{}]".format(i), end=' ') 
            print(self.messages.pop())
            print()
            i += 1
        if self.newlydiscovered:
            print("discovered {} new identities:".format(len(self.newlydiscovered)))
        i = 1
        while self.newlydiscovered:
            print(i, "-", self.newlydiscovered.pop())
            i += 1
        self.updateprompt()
    
    def do_introduce(self, *args):
        """Introduce your own ID. Usage introduce [<id key> ...]."""
        usingseeds = args[0] == ""
        if usingseeds and self.captchas:
            return self.onecmd("solvecaptcha")

        def usecaptchas(captchas):
            cap = captchas.splitlines()
            c = set(cap) # avoid duplicates
            # shuffle all new captchas, but not the old ones
            self.captchas = [i for i in self.captchas
                             if i not in c]
            random.shuffle(cap)
            self.captchas.extend(cap)
            return self.onecmd("solvecaptcha")
        
        if usingseeds and self.captchaiters:
            for captchaiter in self.captchaiters[:]:
                try:
                    captchas = next(captchaiter)
                except StopIteration: # captchaiter is finished, nothing more to gain
                    self.captchhaiters.remove(captchaiter)
                else:
                    if captchas is not None:
                        return usecaptchas(captchas)
            return

        if usingseeds:
            ids = [i.split("@")[1].split(",")[0] for i in self.seedkeys]
            keys = self.seedkeys
            trustifmissing = self.seedtrust
            commentifmissing = "Automatically assigned trust to a seed identity."
        else:
            try:
                ids = [i.split("@")[1].split(",")[0] for i in args[0].split()]
            except IndexError:
                print("Invalid id key. Interpreting as ID")
                try:
                    ids = args[0].split()
                    keys = [getrequestkey(i, self.identity) for i in args[0].split()]
                except fcp.FCPProtocolError as e:
                    if len(ids) == 1:
                        print("Cannot retrieve request uri for identity {} - please give a requestkey like {}".format(
                            ids[0], self.seedkeys[0]))
                    else:
                        print("Cannot retrieve request uris for the identities {} - please give requestkeys like {}".format(
                            ids, self.seedkeys[0]))
                    print("Reason: {}".format(e))
                    return
            keys = args[0].split()
            trustifmissing = 0
            commentifmissing = "babcom introduce"

        # store the iterator. If there is at least one captcha in it
        captchaiter = prepareintroduce(ids, keys, self.identity, trustifmissing, commentifmissing)
        try:
            captchas = next(captchaiter)
        except StopIteration:
            pass # iteration finished
        else:
            self.captchaiters.append(captchaiter)
            if captchas is not None:
                return usecaptchas(captchas)

    def do_solvecaptcha(self, *args):
        """Solve a captcha. Usage: solvecaptcha [captcha]"""
        if args and args[0].strip():
            captcha = args[0].strip()
        else:
            if not self.captchas:
                print("no captchas available. Please run introduce.")
                return
            # choose at random from the newest 20 captchas, because
            # pop() after shuffle(l) gave too many repetitions, which
            # seems pretty odd.
            captcha = random.choice(self.captchas[-20:])
            self.captchas.remove(captcha)
        print("Please solve the following CAPTCHA to introduce your identity.")
        try:
            question = captcha.split(" with ")[1]
        except IndexError:
            print("broken CAPTCHA", captcha, "Please run introduce.")
            return
        
        solution = input(question + ": ").strip() # strip away spaces
        while solution == "":
            # catch accidentally hitting enter
            print("Received empty solution. Please type a solution to introduce.")
            solution = input(question + ": ").strip() # strip away spaces
        try:
            captchakey = solvecaptcha(captcha, self.identity, solution)
            print("Inserted own identity to {}".format(captchakey))
        except Exception as e:
            captchakey = _captchasolutiontokey(captcha, solution)
            print("Could not insert identity to {}:\n    {}\n".format(captchakey, e))
            print("Run introduce again to try a different CAPTCHA")

    def do_visibleto(self, *args):
        """Check whether the other can currently see me. Usage: visibleto ID
        Example: visibleto FZynnK5Ngi6yTkBAZXGbdRLHVPvQbd2poW6DmZT8vbs"""
        # TODO: allow using nicknames.
        if args[0] == "":
            print("visibleto needs an ID")
            self.onecmd("help visibleto")
            return
        other = args[0].split()[0]
        # remove name or keypart
        other = identityfrom(other)
        # check whether we’re visible for the otherone
        visible = checkvisible(self.identity, other)
        if visible is None:
            print("We do not know whether", other, "can see you.")
            print("There is no explicit trust but there might be propagating trust.")
            # TODO: check whether I can get the score the other sees for me.
        if visible is False:
            print(other, "marked you as spammer and cannot see anything from you.")
        if visible is True:
            print("You are visible to {}: there is explicit trust.".format(other))
            
    def do_known(self, *args):
        """List all known identities."""
        for i in gettrustees(self.identity):
            name, info = getidentity(i, self.identity)
            print(name+"@"+i)

    def do_exec(self, *args):
        """Execute the code entered. WARNING! Only for development purposes!"""
        code = [args[0]]
        if args[0] == "":
            print("# type your code. Finish with an empty line.")
        nextline = input()
        while nextline != "":
            code.append(nextline)
            nextline = input()
        exec("\n".join(code))
            
    def do_contact(self, *args):
        """Contact someone by private message (Freemail). Usage: contact USK@..."""
        otherkey = args[0]
        otherid = identityfrom(otherkey)
        trustifmissing = 0
        commentifmissing = "Trust received from solving a CAPTCHA" # fake captcha to hide traces. TODO: find a way to set hidden trust 0
        ensureavailability(otherid, otherkey, self.identity,
                           trustifmissing=trustifmissing,
                           commentifmissing=commentifmissing)
        name, info = getidentity(otherid, self.identity)
        
        send_freemail(self.username + "@" + self.identity,
                      name + "@" + otherid,
                      "test")
            
    def do_hello(self, *args):
        """Says Hello. Usage: hello [<name>]"""
        name = args[0] if args else 'World'
        print("Hello {}".format(name))

    def do_quit(self, *args):
        "Leaves the program"
        [i.cancel() for i in self.timers]
        self.save()
        raise SystemExit

    def do_EOF(self, *args):
        "Leaves the program. Commonly called via CTRL-D"
        [i.cancel() for i in self.timers]
        self.save()
        raise SystemExit

    def emptyline(self, *args):
        "What is done for an empty line"
        print("Type help and hit enter to get help")


class ProtocolError(Exception):
    """
    Did not get the expected reply.
    """
    

def _parse_name(wot_identifier):
    """
    Parse identifier of the forms: nick
                                   nick@key
                                   @key
    :Return: nick, key. If a part is not given return an empty string for it.
    
    >>> _parse_name("BabcomTest@123")
    ('BabcomTest', '123')
    """
    split = wot_identifier.split('@', 1)
    nickname_prefix = split[0]
    key_prefix = (split[1] if split[1:] else '')
    return nickname_prefix, key_prefix


@withprogress
def wotmessage(messagetype, **params):
    """Send a message to the Web of Trust plugin

    >>> name = wotmessage("RandomName")["Replies.Name"]
    """
    params["Message"] = messagetype
    
    def sendmessage(params):
        with fcp.FCPNode() as n:
            return n.fcpPluginMessage(plugin_name="plugins.WebOfTrust.WebOfTrust",
                                      plugin_params=params)[0]
    try:
        resp = sendmessage(params)
    except fcp.FCPProtocolError as e:
        if str(e) == "ProtocolError;No such plugin":
            logging.warn("Plugin Web Of Trust not loaded. Trying to load it.")
            with fcp.FCPNode() as n:
                jobid = n._getUniqueId()
                resp = n._submitCmd(jobid, "LoadPlugin",
                                    PluginURL="WebOfTrust",
                                    URLType="official",
                                    OfficialSource="freenet")[0]
            resp = sendmessage(params)
        else: raise
    return resp


def ensureofficialpluginloaded(pluginname):
    """Ensure that the given plugin is loaded.

    :param pluginname: Freemail
    """
    try:
        with fcp.FCPNode() as n:
            jobid = n._getUniqueId()
            resp = n._submitCmd(jobid, "GetPluginInfo",
                                PluginName=pluginname)[0]
    except fcp.FCPProtocolError as e:
        if str(e) == "ProtocolError;No such plugin":
            logging.warn("Plugin " + pluginname + " not loaded. Trying to load it.")
            with fcp.FCPNode() as n:
                jobid = n._getUniqueId()
                resp = n._submitCmd(jobid, "LoadPlugin",
                                    PluginURL=pluginname,
                                    URLType="official",
                                    OfficialSource="freenet")[0]
        else: raise
    
    return resp



def randomname():
    return wotmessage("RandomName")["Replies.Name"]
        

def createidentity(name="BabcomTest", removedefaultseeds=True):
    """Create a new Web of Trust identity.

    >>> name = "BabcomTest"
    >>> if slowtests:
    ...     createidentity(name)
    ... else: name
    'BabcomTest'
    
    :returns: the name of the identity created.
    """
    if not name:
        name = wotmessage("RandomName")["Name"]
    resp = wotmessage("CreateIdentity", Nickname=name, Context="babcom", # context cannot be empty
                      PublishTrustList="true", # must use string "true"
                      PublishIntroductionPuzzles="true")
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'IdentityCreated':
        raise ProtocolError(resp)
    # prune seed-trust, since babcom does its own bootstrapping.
    # TODO: consider changing this when we add support for other services.
    if removedefaultseeds:
        identity = resp['Replies.ID']
        for trustee in gettrustees(identity):
            removetrust(identity, trustee)
    
    return name


def parseownidentitiesresponse(response):
    """Parse the response to Get OwnIdentities from the WoT plugin.

    :returns: [(name, {InsertURI: ..., ...}), ...]

    >>> resp = parseownidentitiesresponse({'Replies.Nickname0': 'FAKE', 'Replies.RequestURI0': 'USK@...', 'Replies.InsertURI0': 'USK@...', 'Replies.Identity0': 'fVzf7fg0Va7vNTZZQNKMCDGo6-FSxzF3PhthcXKRRvA', 'Replies.Message': 'OwnIdentities', 'Success': 'true', 'header': 'FCPPluginReply', 'Replies.Properties0.Property0.Name': 'fake', 'Replies.Properties0.Property0.Value': 'true'})
    >>> list(sorted([(name, list(sorted(info.items()))) for name, info in resp]))
    [('FAKE', [('Contexts', []), ('Identity', 'fVzf7fg0Va7vNTZZQNKMCDGo6-FSxzF3PhthcXKRRvA'), ('InsertURI', 'USK@...'), ('Properties', {'fake': 'true'}), ('RequestURI', 'USK@...'), ('id_num', '0')])]
    """
    field = "Replies.Nickname"
    identities = []
    for i in response:
        if i.startswith(field):
            # format: Replies.Nickname<id_num>
            id_num = i[len(field):]
            nickname = response[i]
            pubkey_hash = response['Replies.Identity{}'.format(id_num)]
            request = response['Replies.RequestURI{}'.format(id_num)]
            insert = response['Replies.InsertURI{}'.format(id_num)]
            contexts = [response[j] for j in response if j.startswith("Replies.Contexts{}.Context".format(id_num))]
            property_keys_keys = [j for j in sorted(response.keys())
                                  if (j.startswith("Replies.Properties{}.Property".format(id_num))
                                      and j.endswith(".Name"))]
            property_value_keys = [j for j in sorted(response.keys())
                                   if (j.startswith("Replies.Properties{}.Property".format(id_num))
                                       and j.endswith(".Value"))]
            properties = dict((response[j], response[k]) for j,k in zip(property_keys_keys, property_value_keys))
            identities.append((nickname, {"id_num": id_num, "Identity":
                                          pubkey_hash, "RequestURI": request, "InsertURI": insert,
                                          "Contexts": contexts, "Properties": properties}))
    return identities


def parseidentityresponse(response):
    """Parse the response to Get OwnIdentities from the WoT plugin.

    :returns: [(name, {InsertURI: ..., ...}), ...]

    >>> resp = {'Replies.Identity': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'Replies.Identities.0.CurrentEditionFetchState': 'Fetched', 'Replies.Identities.0.Property0.Name': 'IntroductionPuzzleCount', 'Replies.Property0.Value': 10, 'Replies.Rank': 'null', 'Replies.Identities.0.Contexts.0.Name': 'babcom', 'Replies.Identities.0.Property0.Value': 10, 'Replies.Properties0.Property0.Name': 'IntroductionPuzzleCount', 'header': 'FCPPluginReply', 'Replies.Type': 'OwnIdentity', 'Replies.Identities.0.Nickname': 'BabcomTest_other', 'Replies.ID': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'Replies.Identities.0.Type': 'OwnIdentity', 'Replies.Message': 'Identity', 'Replies.Contexts0.Amount': 2, 'Replies.Identity0': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'Replies.Identities.Amount': 1, 'Replies.Scores.0.Value': 'Nonexistent', 'Replies.PublishesTrustList0': 'true', 'Replies.RequestURI': 'USK@jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls,-6yAY9Qq2YildfGRFikIsWQf6RDzPAc84q-gPcbXR7o,AQACAAE/WebOfTrust/1', 'Replies.VersionID': 'a60e2f40-d5e0-4069-8297-05ce8819d817', 'Replies.ID0': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'Replies.Score0': 'null', 'Replies.CurrentEditionFetchState0': 'Fetched', 'Replies.Contexts0.Context1': 'Introduction', 'Replies.Rank0': 'null', 'Replies.Identities.0.PublishesTrustList': 'true', 'Replies.Contexts.1.Name': 'Introduction', 'Replies.Properties0.Amount': 1, 'Replies.Trust0': 'null', 'Replies.Nickname': 'BabcomTest_other', 'Replies.Identities.0.Properties.0.Value': 10, 'Replies.Properties.0.Value': 10, 'Replies.Score': 'null', 'Replies.Trusts.0.Value': 'Nonexistent', 'Replies.Identities.0.VersionID': '118044f9-0ee8-4986-b798-d0645779ac1b', 'Replies.Properties.0.Name': 'IntroductionPuzzleCount', 'Replies.Context1': 'Introduction', 'Replies.Context0': 'babcom', 'Success': 'true', 'Replies.VersionID0': '84e4aba7-ebfe-4ee6-884f-ea7275decc7b', 'Replies.CurrentEditionFetchState': 'Fetched', 'Replies.Contexts.Amount': 2, 'Replies.Contexts0.Context0': 'babcom', 'Replies.Identities.0.Contexts.1.Name': 'Introduction', 'Replies.Trust': 'null', 'Replies.Properties0.Property0.Value': 10, 'Replies.PublishesTrustList': 'true', 'Identifier': 'id2342652746084203', 'Replies.Nickname0': 'BabcomTest_other', 'Replies.Property0.Name': 'IntroductionPuzzleCount', 'Replies.Contexts.0.Name': 'babcom', 'Replies.Type0': 'OwnIdentity', 'Replies.Properties.Amount': 1, 'Replies.Identities.0.ID': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'PluginName': 'plugins.WebOfTrust.WebOfTrust', 'Replies.Identities.0.Identity': 'jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls', 'Replies.Identities.0.RequestURI': 'USK@jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls,-6yAY9Qq2YildfGRFikIsWQf6RDzPAc84q-gPcbXR7o,AQACAAE/WebOfTrust/1', 'Replies.Identities.0.Context0': 'babcom', 'Replies.Identities.0.Context1': 'Introduction', 'Replies.Identities.0.Properties.0.Name': 'IntroductionPuzzleCount', 'Replies.Identities.0.Contexts.Amount': 2, 'Replies.Identities.0.Properties.Amount': 1, 'Replies.RequestURI0': 'USK@jFEicE8bMY0pBN4x6VaN8PsCW342VuuTr0hAc0t39Ls,-6yAY9Qq2YildfGRFikIsWQf6RDzPAc84q-gPcbXR7o,AQACAAE/WebOfTrust/1'}
    >>> name, info = parseidentityresponse(resp)
    >>> name
    'BabcomTest_other'
    >>> info['RequestURI'].split(",")[-1]
    'AQACAAE/WebOfTrust/1'
    >>> list(sorted(info.keys()))
    ['Contexts', 'CurrentEditionFetchState', 'Identity', 'Properties', 'RequestURI']
    """
    fetchedstate = response["Replies.CurrentEditionFetchState"]
    if fetchedstate != "NotFetched":
        nickname = response["Replies.Nickname"]
    else:
        nickname = None
    pubkey_hash = response['Replies.Identity']
    request = response['Replies.RequestURI']
    contexts = [response[j] for j in response if j.startswith("Replies.Contexts.Context")]
    property_keys_keys = [j for j in sorted(response.keys())
                          if (j.startswith("Replies.Properties")
                              and j.endswith(".Name"))]
    property_value_keys = [j for j in sorted(response.keys())
                           if (j.startswith("Replies.Properties")
                               and j.endswith(".Value"))]
    properties = dict((response[j], response[k]) for j,k in zip(property_keys_keys, property_value_keys))
    info = {"Identity": pubkey_hash, "RequestURI": request,
            "Contexts": contexts, "Properties": properties,
            "CurrentEditionFetchState": fetchedstate}
    return nickname, info


def _requestallownidentities():
    """Get all own identities.

    >>> resp = _requestallownidentities()
    >>> matching = _matchingidentities("BabcomTest", resp)
    >>> # [(name, info) for name, info in matching]
    """
    resp = wotmessage("GetOwnIdentities")
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', '') != 'OwnIdentities':
        raise ProtocolError(resp)
    return resp

    
def _matchingidentities(prefix, response):
    """Find matching identities in a Web of Trust Plugin response.

    >>> _matchingidentities("BabcomTest", {})
    []
    """
    identities = parseownidentitiesresponse(response)
    nickname_prefix, key_prefix = _parse_name(prefix)
    matches = [(name, info) for name,info in identities
               if (info["Identity"].startswith(key_prefix) and
                   name.startswith(nickname_prefix))]
    # sort the matches by smallest difference to the prefix so that an
    # exact match of the nickname always wins against longer names.
    return sorted(matches, key=lambda match: len(match[0]) - len(nickname_prefix))


def getownidentities(user):
    """Get all own identities which match user."""
    resp = _requestallownidentities()
    return _matchingidentities(user, resp)


def myidentity(user=None):
    """Get an identity from the Web of Trust plugin.

    :param user: Name of the Identity, optionally with additional
                 prefix of the key to disambiguate it.

    If there are multiple IDs matching the name, the user has to
    disambiguate them by selecting one or by adding parts of the
    identity key to the name.

    :returns: [(name, info), ...]
    
    >>> matches = myidentity("BabcomTest")
    >>> matches[0][0]
    'BabcomTest'

    """
    if user is None:
        user = createidentity()
    matches = getownidentities(user)
    if not matches:
        createidentity(user)
        matches = getownidentities(user)
        
    return matches


def getidentity(identity, truster):
    """Get all own identities which match user.

    >>> othername = "BabcomTest_other"
    >>> if slowtests:
    ...     matches = myidentity("BabcomTest")
    ...     name, info = matches[0]
    ...     truster = info["Identity"]
    ...     matches = myidentity(othername)
    ...     name, info = matches[0]
    ...     identity = info["Identity"]
    ...     name, info = getidentity(identity, truster)
    ...     name
    ... else: othername
    'BabcomTest_other'
    """
    resp = wotmessage("GetIdentity",
                      Identity=identity, Truster=truster)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', '') != 'Identity':
        raise ProtocolError(resp)

    name, info = parseidentityresponse(resp)
    return name, info


def addcontext(identity, context):
    """Add a context to an identity to show others that it supports a certain service.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> addcontext(identity, "testadd")
    >>> matches = myidentity(name)
    >>> info = matches[0][1]
    >>> "testadd" in info["Contexts"]
    True
    """
    resp = wotmessage("AddContext",
                      Identity=identity,
                      Context=context)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', '') != 'ContextAdded':
        raise ProtocolError(resp)
    

def removecontext(identity, context):
    """Add a context to an identity to show others that it supports a certain service.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> addcontext(identity, "testremove")
    >>> removecontext(identity, "testremove")
    >>> removecontext(identity, "testadd")
    """
    resp = wotmessage("RemoveContext",
                      Identity=identity,
                      Context=context)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', '') != 'ContextRemoved':
        raise ProtocolError(resp)
    

def ssktousk(ssk, foldername):
    """Convert an SSK to a USK.

    >>> ssktousk("SSK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/", "folder")
    'USK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/folder/0'
    """
    return "".join(("U", ssk[1:].split("/")[0],
                    "/", foldername, "/0"))


@withprogress
def fastput(private, data, node=None):
    """Upload a small amount of data as fast as possible.

    :param data: a string of bytes. Take care to encode strings to utf-8!

    >>> with fcp.FCPNode() as n:
    ...    pub, priv = n.genkey(name="hello.txt")
    >>> if slowtests:
    ...     pubtoo = fastput(priv, b"Hello Friend!")
    >>> with fcp.FCPNode() as n:
    ...    pub, priv = n.genkey()
    ...    insertusk = ssktousk(priv, "folder")
    ...    data = b"Hello USK"
    ...    if slowtests:
    ...        pub = fastput(insertusk, data, node=n)
    ...        dat = fastget(pub)[1].decode("utf-8")
    ...    else: 
    ...        pub = "something,AQACAAE/folder/0"
    ...        dat = data
    ...    pub.split(",")[-1], dat
    ('AQACAAE/folder/0', b'Hello USK')
    """
    def n():
        if node is None:
            return fcp.FCPNode()
        return node
    with n() as node:
        return node.put(uri=private, data=data,
                        mimetype="application/octet-stream",
                        realtime=True, priority=1)


@withprogress
def fastget(public, node=None):
    """Download a small amount of data as fast as possible.

    :param public: the (public) key of the data to fetch.

    :returns: the data the key references as bytes (decode to utf-8 to get a string).

    On failure it raises an Exception.

    Note: only use this for small files. For large files it is slower
    than regular node.get() and might block other usage of the node.

    >>> with fcp.FCPNode() as n:
    ...    pub, priv = n.genkey(name="hello.txt")
    ...    data = b"Hello Friend!"
    ...    if slowtests:
    ...        pubkey = fastput(priv, data, node=n)
    ...        fastget(pub, node=n)[1]
    ...    else: data
    b'Hello Friend!'

    """
    def n():
        if node is None:
            return fcp.FCPNode()
        return node
    with n() as node:
        return node.get(public,
                        realtime=True, priority=1,
                        followRedirect=True)


def getinsertkey(identity):
    """Get the insert key of the given identity.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> insertkey = getinsertkey(identity)
    >>> insertkey.split("/")[0].split(",")[-1]
    'AQECAAE'
    """
    resp = _requestallownidentities()
    identities = parseownidentitiesresponse(resp)
    insertkeys = [info["InsertURI"]
                  for name,info in identities
                  if info["Identity"] == identity]
    if insertkeys[1:]:
        raise ProtocolError(
            "More than one insert key for the same identity: {}".format(
                insertkeys))
    return insertkeys[0]


def getrequestkey(identity, truster):
    """Get the request key of the given identity.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> requestkey = getrequestkey(identity, identity)
    >>> requestkey.split("/")[0].split(",")[-1]
    'AQACAAE'
    """
    name, info = getidentity(identity, truster)
    requestkey = info["RequestURI"]
    return requestkey


def identityfrom(identitykey):
    """Get the identity for the key.

    :param identitykey: name@identity, insertkey or requestkey: USK@...,...,AQ.CAAE/WebOfTrust/N
    
    >>> identityfrom("USK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/WebOfTrust/0")
    'pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444'
    """
    if "@" in identitykey:
        identitykey = identitykey.split("@")[1]
    if "/" in identitykey:
        identitykey = identitykey.split("/")[0]
    if "," in identitykey:
        identitykey = identitykey.split(",")[0]
    return identitykey


def createcaptchas(number=20, seed=None):
    """Create text captchas

    >>> createcaptchas(number=1, seed=42)
    [('KSK@sJBz_USRK_zHvz_? with 38 plus 28 = ?', 'sJBz_USRK_zHvz_66')]
    
    :returns: [(captchatext, solution), ...]
    """
    # prepare the random number generator for reproducible tests.
    random.seed(seed)
    
    def plus(x, y):
        "KSK@{2}? with {0} plus {1} = ?"
        return x + y
        
    def minus(x, y):
        "KSK@{2}? with {0} minus {1} = ?"
        return x - y
        
    def plusequals(x, y):
        "KSK@{2}? with {0} plus ? = {1}"
        return y - x
        
    def minusequals(x, y):
        "KSK@{2}? with {0} minus ? = {1}"
        return x + y
        
    questions = [plus, minus,
                 plusequals,
                 minusequals]

    captchas = []
    
    def fourletters():
        return [random.choice("ABCDEFHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
                for i in range(4)]
    
    def secret():
        return "".join(fourletters() + ["_"] +
                       fourletters() + ["_"] +
                       fourletters() + ["_"])
    
    for i in range(number):
        sec = secret()
        question = random.choice(questions)
        x = random.randint(1, 49)
        y = random.randint(1, 49)
        captcha = question.__doc__.format(x, y, sec)
        solution = sec + str(question(x, y))
        captchas.append((captcha, solution))

    return captchas


def getcaptchausk(identitykey):
    """Turn a regular identity key (request or insert) into a captcha key.

    >>> fromssk = getcaptchausk("SSK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/")
    >>> fromusk = getcaptchausk("USK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/WebOfTrust/0")
    >>> fromrawssk = getcaptchausk("SSK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE")
    >>> fromsskfile = getcaptchausk("SSK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/file.txt")
    >>> fromssk == fromusk == fromrawssk == fromsskfile
    True
    >>> fromssk
    'USK@pAOgyTDft8bipMTWwoHk1hJ1lhWDvHP3SILOtD1e444,Wpx6ypjoFrsy6sC9k6BVqw-qVu8fgyXmxikGM4Fygzw,AQACAAE/babcomcaptchas/0'
    """
    rawkey = identitykey.split("/")[0]
    ssk = "S" + rawkey[1:]
    return ssktousk(ssk, "babcomcaptchas")
    

def insertcaptchas(identity):
    """Insert a list of CAPTCHAs.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> if slowtests:
    ...     usk, solutions = insertcaptchas(identity)
    ...     solutions[0][:4]
    ... else: "KSK@"
    'KSK@'

    :returns: captchasuri, ["KSK@solution", ...]
    """
    insertkey = getinsertkey(identity)
    captchas = createcaptchas()
    captchasdata = "\n".join(captcha for captcha,solution in captchas)
    captchasolutions = [solution for captcha,solution in captchas]
    captchausk = getcaptchausk(insertkey)
    with fcp.FCPNode() as n:
        pub = fastput(captchausk, captchasdata.encode("utf-8"), node=n)
    return pub, ["KSK@" + solution
                 for solution in captchasolutions]
    

def providecaptchas(identity):
    """Provide a link to the CAPTCHA queue as property of the identity.

    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> if slowtests:
    ...     solutions = introducecaptchas(identity)
    ...     matches = myidentity("BabcomTest")
    ...     name, info = matches[0]
    ...     "babcomcaptchas" in info["Properties"]
    ... else: True
    True
    
    :returns: ["KSK@...", ...] # the solutions to watch
    """
    pubusk, solutions = insertcaptchas(identity)
    resp = wotmessage("SetProperty", Identity=identity,
                      Property="babcomcaptchas",
                      Value=pubusk)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'PropertyAdded':
        raise ProtocolError(resp)

    return solutions


def _captchasolutiontokey(captcha, solution):
    """Turn the CAPTCHA and its solution into a key.
    
    >>> captcha = 'KSK@hBQM_njuE_XBMb_? with 10 plus 32 = ?'
    >>> solution = '42'
    >>> _captchasolutiontokey(captcha, solution)
    'KSK@hBQM_njuE_XBMb_42'
    """
    secret = captcha.split("?")[0]
    return secret + str(solution)
    

def solvecaptcha(captcha, identity, solution):
    """Use the solution to solve the CAPTCHA.

    >>> captcha = 'KSK@hBQM_njuE_XBMb_? with 10 plus 32 = ?'
    >>> solution = '42'
    >>> matches = myidentity("BabcomTest")
    >>> name, info = matches[0]
    >>> identity = info["Identity"]
    >>> idrequestkey = getrequestkey(identity, identity)
    >>> if slowtests:
    ...     captchakey = solvecaptcha(captcha, identity, solution)
    ...     idrequestkey == fastget(captchakey)[1].decode("utf-8")
    ... else: True
    True
    """
    captchakey = _captchasolutiontokey(captcha, solution)
    idkey = getrequestkey(identity, identity)
    return fastput(captchakey, idkey)


def gettrust(truster, trustee):
    """Set trust to an identity.

    >>> my = myidentity("BabcomTest")[0][1]["Identity"]
    >>> other = myidentity("BabcomTest_other")[0][1]["Identity"]
    >>> gettrust(my, other)
    'Nonexistent'
    """
    resp = wotmessage("GetTrust",
                      Truster=truster, Trustee=trustee)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'Trust':
        raise ProtocolError(resp)
    return resp['Replies.Trusts.0.Value']


def settrust(myidentity, otheridentity, trust, comment):
    """Set trust to an identity.

    :param trust: -100..100. 
                  -100 to -2: report as spammer, do not download.
                  -1: do not download.
                   0: download and show.
                   1 to 100: download, show and mark as non-spammer so
                       others download the identity, too.
    """
    resp = wotmessage("SetTrust",
                      Truster=myidentity, Trustee=otheridentity,
                      Value=str(trust), Comment=comment)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'TrustSet':
        raise ProtocolError(resp)


def removetrust(myidentity, otheridentity):
    """Remove the trust of an identity."""
    resp = wotmessage("RemoveTrust",
                      Truster=myidentity, Trustee=otheridentity)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'TrustRemoved':
        raise ProtocolError(resp)


def addidentity(requesturi):
    """Ensure that WoT knows the given identity."""
    resp = wotmessage("AddIdentity",
                      RequestURI=requesturi)
    if resp['header'] != 'FCPPluginReply' or resp.get('Replies.Message', "") != 'IdentityAdded':
        raise ProtocolError(resp)


def watchcaptchas(solutions):
    """Watch the solutions to the CAPTCHAs
    
    :param solutions: Freenet Keys where others can upload solved CAPTCHAs. 

    :returns: generator which yields None or (key, data).
              <generator with ('KSK@...<captchakey>', 'USK@...<identity>')...>

    # TODO: check whether returning (None, None) or (key, data) 
            would lead to better code.

    Just call watcher = watchcaptchas(solutions), then you can ask
    watcher whether there’s a solution via watcher.next(). It should
    return after roughly 10ms, either with None or with (key, data)
    
    >>> d1 = b"Test"
    >>> d2 = b"Test2"
    >>> k1 = "KSK@tcshrietshcrietsnhcrie-Test"
    >>> k2 = "KSK@tcshrietshcrietsnhcrie-Test2"
    >>> if slowtests or True:
    ...     k1res = fastput(k1, d1)
    ...     k2res = fastput(k2, d2)
    ...     watcher = watchcaptchas([k1,k2])
    ...     [i for i in watcher if i is not None] # drain watcher.
    ...     # note: I cannot use i.next() in the doctest, else I’d get "I/O operation on closed file"
    ... else:
    ...     [(k1, d1), (k2, d2)]
    [('KSK@tcshrietshcrietsnhcrie-Test', bytearray(b'Test')), ('KSK@tcshrietshcrietsnhcrie-Test2', bytearray(b'Test2'))]

    """
    # TODO: in Python3 this could be replaced with less than half the lines using futures.
    # use a shared fcp connection for all get requests
    node = fcp.FCPNode()
    tasks = []
    for i in solutions:
        job = node.get(i,
                       realtime=True, priority=4,
                       followRedirect=False,
                       async=True)
        tasks.append((i, job))
    
    while tasks:
        atleastone = False
        for key, job in tasks:
            if job.isComplete():
                tasks.remove((key, job))
                atleastone = True
                res = key, job.getResult()[1]
                yield res
        if not atleastone:
            yield None # no job finished
    
    # close the node. Use a new one for the next run.
    node.shutdown()


def ensureavailability(identity, requesturi, ownidentity, trustifmissing, commentifmissing):
    """Ensure that the given identity is available in the WoT, adding trust as necessary.
    
    :returns: True if trust had to be added, else False
    """
    try:
        name, info = getidentity(identity, ownidentity)
    except ProtocolError as e:
        unknowniderror = 'plugins.WebOfTrust.exceptions.UnknownIdentityException: {}'.format(identity)
        if e.args[0]['Replies.Description'] == unknowniderror:
            logging.warn("identity {} not yet known. Adding trust {}".format(identity, trustifmissing))
            addidentity(requesturi)
            settrust(ownidentity, identity, trustifmissing, commentifmissing)
            return True
        else:
            raise
    return False
                    
                
def prepareintroduce(identities, requesturis, ownidentity, trustifmissing, commentifmissing):
    """Prepare announcing to the identities.

    This ensures that the identity is known to WoT, gives it trust to
    ensure that it will be fetched, pre-fetches the ID and fetches the
    captchas. It returns an iterator which yields either a captcha to
    solve or None.
    """
    # ensure that we have a real copy to avoid mutating the original lists.
    ids = identities[:]
    keys = requesturis[:]
    tasks = list(zip(ids, keys))
    # use a single node for all the the get requests in the iterator.
    node = fcp.FCPNode()
    while tasks:
        for identity, requesturi in tasks[:]:
            ensureavailability(identity, requesturi, ownidentity, trustifmissing, commentifmissing)
            try:
                print("Getting identity information for {}".format(identity))
                name, info = getidentity(identity, ownidentity)
            except ProtocolError as e:
                unknowniderror = 'plugins.WebOfTrust.exceptions.UnknownIdentityException: {}'.format(identity)
                if e.args[0]['Replies.Description'] == unknowniderror:
                    logging.warn("identity to introduce not yet known. Adding trust {} for {}".format(trustifmissing, identity))
                    addidentity(requesturi)
                    settrust(ownidentity, identity, trustifmissing, commentifmissing)
                name, info = getidentity(identity, ownidentity)
            if "babcomcaptchas" in info["Properties"]:
                print("Getting CAPTCHAs for id", identity)
                captchas = fastget(info["Properties"]["babcomcaptchas"],
                                   node=node)[1].decode("utf-8")
                # task finished
                tasks.remove((identity, requesturi))
                yield captchas
            else:
                if info["CurrentEditionFetchState"] == "NotFetched":
                    print("Cannot introduce to identity {}, because it has not been fetched, yet.".format(identity))
                    trust = gettrust(ownidentity, identity)
                    if trust == "Nonexistent" or int(trust) >= 0:
                        if trust == "Nonexistent":
                            print("No trust set yet. Setting trust", trustifmissing, "to ensure that identity {} gets fetched.".format(identity))
                            settrust(ownidentity, identity, trustifmissing, commentifmissing)
                        else:
                            print("The identity has trust {}, so it should be fetched soon.".format(trust))
                        print("firing get({}) in background to make it more likely that the ID is fetched quickly (since it’s already in the local store, then).".format(requesturi))
                        node.get(requesturi, followRedirect=True,
                                 async=True, persistence="reboot", nodata=True)
                        # use the captchas without going through Web of Trust to avoid a slowpath
                        print("Getting the captchas from {}".format(requesturi))
                        captchas = fastget(getcaptchausk(requesturi),
                                           node=node).decode("utf-8")
                        # task finished
                        tasks.remove((identity, requesturi))
                        yield captchas
                    else:
                        print("You marked this identity as spammer or disruptive by setting trust {}, so it cannot be fetched.".format(trust))
                        # task finished: it cannot be done
                        tasks.remove((identity, requesturi))
                else:
                    name, info = getidentity(identity, ownidentity)
                    # try to go around WoT
                    captchausk = getcaptchausk(info["RequestURI"])
                    try:
                        yield fastget(captchausk,
                                      node=node)[1].decode("utf-8")
                    except Exception as e:
                        print("Identity {}@{} published no CAPTCHAs, cannot introduce to it.".format(name, identity))
                        print("reason:", e)
                    tasks.remove((identity, requesturi))
    # close the FCP connection when all tasks are done.
    node.shutdown()


def parsetrusteesresponse(response):
    """Parse the response to GetTrustees from the WoT plugin.

    :returns: [(name, {InsertURI: ..., ...}), ...]

    >>> resp = parseownidentitiesresponse({'Replies.Nickname0': 'FAKE', 'Replies.RequestURI0': 'USK@...', 'Replies.InsertURI0': 'USK@...', 'Replies.Comment0': 'Fake', 'Replies.Identity0': 'fVzf7fg0Va7vNTZZQNKMCDGo6-FSxzF3PhthcXKRRvA', 'Replies.Message': 'dentities', 'Success': 'true', 'header': 'FCPPluginReply', 'Replies.Properties0.Property0.Name': 'fake', 'Replies.Properties0.Property0.Value': 'true'})
    >>> [(name, list(sorted(info.items()))) for name, info in resp]
    [('FAKE', [('Contexts', []), ('Identity', 'fVzf7fg0Va7vNTZZQNKMCDGo6-FSxzF3PhthcXKRRvA'), ('InsertURI', 'USK@...'), ('Properties', {'fake': 'true'}), ('RequestURI', 'USK@...'), ('id_num', '0')])]
    """
    field = "Replies.Identity"
    identities = []
    for i in response:
        if i.startswith(field):
            # format: Replies.Nickname<id_num>
            id_num = i[len(field):]
            pubkey_hash = response['Replies.Identity{}'.format(id_num)]
            request = response['Replies.RequestURI{}'.format(id_num)]
            nickname = response.get("Replies.Nickname{}".format(id_num), None)
            comment = response.get("Replies.Comment{}".format(id_num), None)
            contexts = [response[j] for j in response if j.startswith("Replies.Contexts{}.Context".format(id_num))]
            property_keys_keys = [j for j in sorted(response.keys())
                                  if (j.startswith("Replies.Properties{}.Property".format(id_num))
                                      and j.endswith(".Name"))]
            property_value_keys = [j for j in sorted(response.keys())
                                   if (j.startswith("Replies.Properties{}.Property".format(id_num))
                                       and j.endswith(".Value"))]
            properties = dict((response[j], response[k]) for j,k in zip(property_keys_keys, property_value_keys))
            identities.append((pubkey_hash, {"id_num": id_num, "Comment": comment, "Nickname":
                                             nickname, "RequestURI": request,
                                             "Contexts": contexts, "Properties": properties}))
    return identities


def gettrustees(identity):
    resp = wotmessage("GetTrustees", Identity=identity,
                      Context="") # any context
    return dict(parsetrusteesresponse(resp))

                    
def checkvisible(ownidentity, otheridentity):
    """Check whether the other identity can see me."""
    # TODO: get the exact trust value
    trustees = gettrustees(otheridentity)
    if ownidentity in trustees:
        return True


# The Freemail functionality is adapted from the Infocalypse work of Steve Dougherty

def send_freemail(from_nickidentity, to_nickidentity, message,
                  password="12345", # FIXME: Use a proper password
                  mailhost="127.0.0.1", smtpport=4025):
    """
    Prompt for a pull request message, and send a pull request from
    from_identity to to_identity for the repository to_repo_name.

    :type to_nickidentity: USER@id
    :type from_nickidentity: USER@id
    """
    setup_freemail(from_nickidentity, mailhost, smtpport)
    from_address = require_freemail(from_nickidentity)
    to_address = require_freemail(to_nickidentity)

    # TODO: Will there always be a request URI set in the config? What about
    # a path? The repo could be missing a request URI, if that URI is
    # set manually. We could check whether the default path is a
    # freenet path. We cannot be sure whether the request uri will
    # always be the uri we want to send the pull-request to, though:
    # It might be an URI we used to get some changes which we now want
    # to send back to the maintainer of the canonical repo.

    # TODO: Save message and load later in case sending fails.

    source_lines = message.splitlines()

    source_lines = [line for line in source_lines]

    # Body is third line and after.
    msg = MIMEText('\n'.join(source_lines[2:]))
    msg['Subject'] = "[babcom] " + source_lines[0]
    msg['To'] = to_address
    msg['From'] = from_address

    host = mailhost
    port = smtpport
    smtp = smtplib.SMTP(host, port)
    smtp.login(from_address, password)
    # TODO: Catch exceptions and give nice error messages.
    smtp.sendmail(from_address, to_address, msg.as_string())


def require_freemail(identity_with_nickname):
    """
    Return the given identity's Freemail address.
    Abort with an error message if the given identity does not have a
    Freemail address / context.

    :param identity_with_nickname: <user>@<identity>
    """
    nickname, identity = _parse_name(identity_with_nickname)
    re_encode = base64.b32encode(fcp.node.base64decode(identity))
    # Remove trailing '=' padding.
    re_encode = re_encode.rstrip('=')
    
    # Freemail addresses are lower case.
    address = nickname + '@' + re_encode  + '.freemail'
    return address.lower()


def setup_freemail(local_id, mailhost, smtpport):
    """
    Test, and set a Freemail password for the identity.

    :returns: password
    """
    ensureofficialpluginloaded("Freemail_wot")
    
    address = require_freemail(local_id)

    # FIXME: use a proper password
    password = "12345"

    host = mailhost
    port = smtpport
    
    # Check that the password works.
    try:
        # TODO: Is this the correct way to get the configured host?
        smtp = smtplib.SMTP(host, port)
        smtp.login(address, password)
    except smtplib.SMTPAuthenticationError as e:
        print("Could not log in with the given password.\nGot '{0}'\n".format(e.smtp_error))
        print("currently you need to visit", "http://" + host + "/Freemail", "and create an account with password", password)
        return
    except smtplib.SMTPConnectError as e:
        print("Could not connect to server.\nGot '{0}'\n".format(e.smtp_error))
        return

    return password


def check_freemail(local_identity, password="12345", # FIXME: use a proper password
                   mailhost="127.0.0.1", imapport=4143):
    """
    Check Freemail for local_identity and print information on any VCS
    messages received.

    :type local_identity: Local_WoT_ID
    """
    address = require_freemail(local_identity)

    # Log in and open inbox.
    host = mailhost
    port = imapport
    imap = imaplib.IMAP4(host, port)
    imap.login(address, password)
    imap.select()

    # Parenthesis to work around erroneous quotes:
    # http://bugs.python.org/issue917120
    reply_type, message_numbers = imap.search(None, '(SUBJECT %s)' % "[babcom]")

    # imaplib returns numbers in a singleton string separated by whitespace.
    message_numbers = message_numbers[0].split()

    if not message_numbers:
        # TODO: Is aborting appropriate here? Should this be ui.status and
        # return?
        print("No messages found.")
        return

    # fetch() expects strings for both. Individual message numbers are
    # separated by commas. It seems desirable to peek because it's not yet
    # apparent that this is a [vcs] message with YAML.
    # Parenthesis to prevent quotes: http://bugs.python.org/issue917120
    status, subjects = imap.fetch(','.join(message_numbers),
                                  r'(body[header.fields Subject])')

    # Expecting 2 list items from imaplib for each message, for example:
    # ('5 (body[HEADER.FIELDS Subject] {47}', 'Subject: [vcs]  ...\r\n\r\n'),
    # ')',

    # Exclude closing parens, which are of length one.
    subjects = [x for x in subjects if len(x) == 2]

    subjects = [x[1] for x in subjects]

    # Match message numbers with subjects; remove prefix and trim whitespace.
    subjects = dict((message_number, subject[len('Subject: '):].rstrip()) for
                    message_number, subject in zip(message_numbers, subjects))

    for message_number, subject in subjects.items():
        status, fetched = imap.fetch(str(message_number),
                                     r'(body[text] '
                                     r'body[header.fields From)')

        # Expecting 3 list items, as with the subject fetch above.
        body = fetched[0][1]
        from_address = fetched[1][1][len('From: '):].rstrip()

        yield from_address, subject, body


    
def _test(verbose=None):

    """Run the tests

    >>> True
    True
    """
    import doctest
    tests = doctest.testmod(verbose=verbose)
    if tests.failed:
        return "☹"*tests.failed + " / " + numtostring(tests.attempted)
    return "^_^ (" + numtostring(tests.attempted) + ")"


if __name__ == "__main__":
    args = parse_args()
    slowtests = args.slowtests
    if args.port:
        fcp.node.defaultFCPPort = args.port
    if args.host:
        fcp.node.defaultFCPHost = args.host
    if args.verbosity:
        fcp.node.defaultVerbosity = int(args.verbosity)
    if args.test:
        print(_test())
        sys.exit(0)
    prompt = Babcom(verbose=args.verbosity >= 4)
    prompt.transient = args.transient
    prompt.username = args.user
    prompt.cmdloop('Starting babcom, type help or intro')
