
# code to deliver build status through twisted.words (instant messaging
# protocols: irc, etc)

import os, re, shlex, glob

from zope.interface import Interface, implements
from twisted.internet import protocol, reactor
from twisted.words.protocols import irc
from twisted.python import log, failure
from twisted.application import internet

from buildbot import interfaces, util
from buildbot import version
from buildbot.sourcestamp import SourceStamp
from buildbot.process.base import BuildRequest
from buildbot.status import base
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, EXCEPTION, SKIPPED
from buildbot import scheduler
from buildbot.steps.shell import ShellCommand

from string import join, capitalize, lower

# Nightly scheduler helper class. It stores the SVN revision of the latest change,
# so it can be used for the builds it is issuing.
class Nightly(scheduler.Nightly):
	def addChange(self, change):
		if change.branch == self.branch:
			self.properties.setProperty("got_revision", change.revision, "Scheduler")
		scheduler.Nightly.addChange(self, change)

# Buildstep class for our build system. A ./configure run is forced on nightly builds,
# which results in a full rebuild. For incremental builds, its only run when no
# Makefile exists (which is generated by the script itself).
class Configure(ShellCommand):
	name = "configure"
	haltOnFailure = 1
	flunkOnFailure = 1
	description = [ "configuring" ]
	descriptionDone = [ "configure" ]
	descriptionDoneSkipped = [ "configure", "(skipped)" ]
	skipped = False

	def start(self):
		properties = self.build.getProperties()

		# buildbot workaround, pass on the revision.
		if properties.has_key("revision"):
			if not properties.has_key("got_revision") or properties["got_revision"] == None:
				self.setProperty("got_revision", properties["revision"])

		self.command = "[ -f Makefile ] || " + " ".join(self.command)

		ShellCommand.start(self)

	def commandComplete(self, cmd):
		if cmd.rc == 0 and len(cmd.logs["stdio"].getText()) < 1:
			self.skipped = True

		ShellCommand.commandComplete(self, cmd)

	def describe(self, done = False):
		if done and self.skipped:
			self.descriptionDone = self.descriptionDoneSkipped

		return ShellCommand.describe(self, done)

# Small buildstep class to strip binaries, only done on nightly builds.
class Strip(ShellCommand):
	name = "strip"
	haltOnFailure = 1
	flunkOnFailure = 1
	description = [ "stripping" ]
	descriptionDone = [ "strip" ]

	def start(self):
		properties = self.build.getProperties()

		if not properties.has_key("package"):
			return SKIPPED

		ShellCommand.start(self)

# Buildstep class to package binaries, only done on nightly builds.
class Package(ShellCommand):
	name = "package"
	haltOnFailure = 1
	flunkOnFailure = 1
	description = [ "packaging" ]
	descriptionDone = [ "package" ]

	def __init__(self, **kwargs):
		self.disttarget = kwargs["disttarget"]
		del kwargs["disttarget"]
		self.srcpath = kwargs["srcpath"]
		del kwargs["srcpath"]
		self.dstpath = kwargs["dstpath"]
		del kwargs["dstpath"]
		self.package = kwargs["package"]
		del kwargs["package"]
		self.buildname = kwargs["buildname"]
		del kwargs["buildname"]
		self.platform_package = kwargs["platform_package"]
		del kwargs["platform_package"]

		ShellCommand.__init__(self, **kwargs)

		self.addFactoryArguments(disttarget = self.disttarget)
		self.addFactoryArguments(srcpath = self.srcpath)
		self.addFactoryArguments(dstpath = self.dstpath)
		self.addFactoryArguments(package = self.package)
		self.addFactoryArguments(buildname = self.buildname)
		self.addFactoryArguments(platform_package = self.platform_package)

	def start(self):
		properties = self.build.getProperties()

		if not properties.has_key("package"):
			return SKIPPED

		disttarget = False

		if len(self.disttarget) > 0:
			disttarget = True

		name = "%s-r%s" % (self.buildname, properties["got_revision"])
		file = "%s.tar.bz2" % name
		symlink = "%s-latest.tar.bz2" % self.buildname

		files = []

		# dont pack up the default files if the port has its own dist target
		if not disttarget:
			files += [ os.path.join(self.srcpath, i) for i in self.package ]

		files += self.platform_package

		self.command = ""

		if disttarget:
			self.command += "make %s && " % self.disttarget

		self.command += "mkdir %s && " % name
		self.command += "cp -r %s %s/ && " % (" ".join(files), name)
		self.command += "tar cvjf %s %s/ && " % (file, name)
		self.command += "mkdir -p %s/ && " % (self.dstpath)
		self.command += "mv %s %s/ && " % (file, self.dstpath)
		self.command += "ln -sf %s %s && " % (file, os.path.join(self.dstpath, symlink))
		self.command += " rm -rf %s || " % name
		self.command +="( rm -rf %s; false )" % name
					
		ShellCommand.start(self)

class Clean(ShellCommand):
	name = "clean"
	haltOnFailure = 1
	flunkOnFailure = 1
	description = [ "cleaning" ]
	descriptionDone = [ "clean" ]

	def __init__(self, **kwargs):
		self.prefix = kwargs["prefix"]
		del kwargs["prefix"]

		ShellCommand.__init__(self, **kwargs)

		self.addFactoryArguments(prefix = self.prefix)

	def start(self):
		self.command = "ccache -C && rm -rf ../../%s-*" % self.prefix
		ShellCommand.start(self)

# IRC stuff

scumm_buildbot_root_url = "http://buildbot.scummvm.org/"

class IrcStatusBot(irc.IRCClient):
	implements(Interface)

	timer = None

	def __init__(self, nickname, password, channel, status, categories, stableTimer):
		self.nickname = nickname
		self.channel = channel
		self.password = password
		self.status = status
		self.categories = categories
		self.stableTimer = stableTimer
		self.delayedSuccess = []
		self.delayedFailure = []
		self.status.subscribe(self)

	silly = {
				"hello": "yes?",
				"hi": "hello"
			}

	def log(self, msg):
		log.msg("%s: %s" % (self, msg))

	def send(self, message):
		self.msg(self.channel, message.encode("ascii", "replace"))

	def act(self, action):
		self.me(self.channel, action.encode("ascii", "replace"))

	def getAllBuilders(self):
		names = self.status.getBuilderNames(categories = self.categories)
		names.sort()
		builders = [self.status.getBuilder(n) for n in names]
		return builders

	def getFailureBuilders(self):
		failure = []
		for b in self.getAllBuilders():
			last = b.getLastFinishedBuild()
			if last != None and last.getResults() == FAILURE:
				failure.append(b.getName())
		return failure

	def buildsetSubmitted(self, buildset):
		self.log('Buildset %s added' % (buildset))

	def builderAdded(self, builderName, builder):
		self.log('Builder %s added' % (builder))
		builder.subscribe(self)

	def builderChangedState(self, builderName, state):
		self.log('Builder %s changed state to %s' % (builderName, state))
		idle = True
		for b in self.getAllBuilders():
			if b.getState()[0] != "idle":
				idle = False
				break

		if not idle:
			return

		if self.timer:
			self.log('All builders are idle, reporting now')
			self.timer.reset(0)

	def requestSubmitted(self, brstatus):
		self.log('BuildRequest for %s submiitted to Builder %s' % 
			(brstatus.getSourceStamp(), brstatus.builderName))

	def builderRemoved(self, builderName):
		self.msg('Builder %s removed' % (builderName))

	def buildStarted(self, builderName, build):
		builder = build.getBuilder()
		self.log('Builder %r in category %s started' % (builder, builder.category))

	def buildFinished(self, builderName, build, results):
		builder = build.getBuilder()

		# only notify about builders we are interested in
		self.log('Builder %r in category %s finished' % (builder, builder.category))

		if (self.categories != None and builder.category not in self.categories):
			return

		result = build.getResults()
		if not result in [ SUCCESS, FAILURE ]:
			return

		prevResult = build.getPreviousBuild().getResults()
		if not prevResult in [ SUCCESS, FAILURE ]:
			return

		if result == prevResult:
			return

		self.log('Delaying status report for builder %r' % builder)

		if result == SUCCESS:
			self.delayedSuccess.append(builder.getName())
		else:
			self.delayedFailure.append(builder.getName())

		if self.timer:
			self.log('Canceling previous status callback')
			self.timer.cancel()
			self.timer = None

		self.timer = reactor.callLater(self.stableTimer,
										self.reportBuildStatus,
										build.getProperty("got_revision"))

	def reportBuildStatus(self, revision):
		self.timer = None

		m = "Port build status changed with r\x02%s\x0f: " % revision

		if len(self.delayedSuccess) > 0:
			m += "\x0303Success\x0f: %s" % ", ".join(self.delayedSuccess)
			self.delayedSuccess = []

		if len(self.delayedFailure) > 0:
			m += "\x0304Failure\x0f: %s" % ", ".join(self.delayedFailure)
			self.delayedFailure = []

		failure = self.getFailureBuilders()
		if len(failure) == 0:
			m += ". Nice work, all ports built fine now"

		m += ". %s" % scumm_buildbot_root_url

		self.send(m)

	def handleAction(self, data, user):
		if not data.endswith("s %s" % self.nickname):
			return

		words = data.split()
		verb = words[-2]

		timeout = 4
		if verb == "kicks":
			response = "%s back" % verb
			timeout = 1
		else:
			response = "%s %s too" % (verb, user)

		reactor.callLater(timeout, self.act, response)

	def handleMessage(self, message, who):
		message = message.lstrip()
		if self.silly.has_key(message):
			return self.doSilly(message)

		parts = message.split(' ', 1)
		if len(parts) == 1:
			parts = parts + ['']
		cmd, args = parts

		meth = self.getCommandMethod(cmd)

		if (meth == None):
			return

		self.log("IRC command '%s' from user '%s'" % (cmd, who))

		error = None
		try:
			meth(args.strip(), who)
		except:
			f = failure.Failure()
			log.err(f)
			error = "Something bad happened (see logs): %s" % f.type

		if error:
			try:
				self.send(error)
			except:
				log.err()

	def doSilly(self, message):
		response = self.silly[message]
		if type(response) != type([]):
			response = [response]
		when = 0.5
		for r in response:
			reactor.callLater(when, self.send, r)
			when += 2.5

	def getCommandMethod(self, command):
		meth = getattr(self, 'command_' + command.upper(), None)
		return meth

	def command_VERSION(self, args, who):
		self.send("buildbot-%s at your service" % version)

	def command_PING(self, args, who):
		self.send("pong")

	def command_STATUS(self, args, who):
		failure = self.getFailureBuilders()

		if len(failure) < 1:
			self.send("Last time I checked, all ports built just fine")
			return

		if len(failure) == 1:
			self.send("%s is currently not building" % failure[0])
			return

		self.send("%d ports are currently not building: %s, see %s" % \
					(len(failure),
					 ", ".join(failure),
					 scumm_buildbot_root_url))

	# the following irc.IRCClient methods are called when we have input

	def privmsg(self, user, channel, message):
		if not channel == self.channel:
			return

		user = user.split('!', 1)[0] # rest is ~user@hostname

		if message.startswith("%s:" % self.nickname) or message.startswith("%s," % self.nickname):
			message = message[len("%s:" % self.nickname):]
			self.handleMessage(message.strip(), user)
		# to track users comings and goings, add code here

	def action(self, user, channel, data):
		data = data.strip()
		user = user.split('!', 1)[0] # rest is ~user@hostname

		# somebody did an action (/me actions) in the broadcast channel
		if self.nickname in data:
			self.handleAction(data, user)

	def signedOn(self):
		if self.password:
			self.msg("Nickserv", "IDENTIFY " + self.password)
		self.join(self.channel)

	def joined(self, channel):
		self.log("I have joined %s" % channel)

	def left(self, channel):
		self.log("I have left %s" % channel)

	def kickedFrom(self, channel, kicker, message):
		self.log("I have been kicked from %s by %s: %s" % (channel,
														  kicker,
														  message))

	# we can using the following irc.IRCClient methods to send output.
	#
	# self.say(channel, message) # broadcast
	# self.msg(user, message) # unicast
	# self.me(channel, action) # send action
	# self.away(message='')
	# self.quit(message='')

class ThrottledClientFactory(protocol.ClientFactory):
	lostDelay = 2
	failedDelay = 60

	def clientConnectionLost(self, connector, reason):
		reactor.callLater(self.lostDelay, connector.connect)

	def clientConnectionFailed(self, connector, reason):
		reactor.callLater(self.failedDelay, connector.connect)

class IrcStatusFactory(ThrottledClientFactory):
	protocol = IrcStatusBot

	status = None
	control = None
	shuttingDown = False
	p = None

	def __init__(self, nickname, password, channel, categories, stableTimer):
		#ThrottledClientFactory.__init__(self) # doesn't exist
		self.status = None
		self.nickname = nickname
		self.password = password
		self.channel = channel
		self.categories = categories
		self.stableTimer = stableTimer

	def __getstate__(self):
		d = self.__dict__.copy()
		del d['p']
		return d

	def shutdown(self):
		self.shuttingDown = True
		if self.p:
			self.p.quit("buildmaster reconfigured: bot disconnecting")

	def buildProtocol(self, address):
		p = self.protocol(self.nickname, self.password,
						  self.channel, self.status,
						  self.categories, self.stableTimer)
		p.factory = self
		p.status = self.status
		p.control = self.control
		self.p = p
		return p

	# TODO: I think a shutdown that occurs while the connection is being
	# established will make this explode

	def clientConnectionLost(self, connector, reason):
		if self.shuttingDown:
			log.msg("not scheduling reconnection attempt")
			return
		ThrottledClientFactory.clientConnectionLost(self, connector, reason)

	def clientConnectionFailed(self, connector, reason):
		if self.shuttingDown:
			log.msg("not scheduling reconnection attempt")
			return
		ThrottledClientFactory.clientConnectionFailed(self, connector, reason)


class IRC(base.StatusReceiverMultiService):
	compare_attrs = ["host", "port", "nick", "password",
					 "channel", "categories"]

	def __init__(self, host, nick, channel, port = 6667, categories = None,
					password = None, stableTimer = 60):
		base.StatusReceiverMultiService.__init__(self)

		# need to stash these so we can detect changes later
		self.host = host
		self.port = port
		self.nick = nick
		self.channel = channel
		self.password = password
		self.categories = categories
		self.stableTimer = stableTimer

		# need to stash the factory so we can give it the status object
		self.f = IrcStatusFactory(self.nick, self.password, self.channel,
									self.categories, self.stableTimer)

		c = internet.TCPClient(host, port, self.f)
		c.setServiceParent(self)

	def setServiceParent(self, parent):
		base.StatusReceiverMultiService.setServiceParent(self, parent)
		self.f.status = parent.getStatus()

	def stopService(self):
		# make sure the factory will stop reconnecting
		self.f.shutdown()
		return base.StatusReceiverMultiService.stopService(self)

