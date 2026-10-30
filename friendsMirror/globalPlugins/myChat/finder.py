# -*- coding: UTF-8 -*-
"""Friends Mirror: search users by name and send a message request."""
import hashlib
import json
import os
import re
import time
import wx
import gui
import ui
import globalVars
import addonHandler
from gui import guiHelper

addonHandler.initTranslation()

USER_RE = re.compile(r"^[a-z0-9_.]{3,24}$")
INDEX_TOPIC = "mcfi" + hashlib.sha256(b"myChat finder user index v1").hexdigest()[:28]
REPUBLISH_EVERY = 3 * 3600   # the relay keeps messages for 12 hours
CFG = os.path.join(globalVars.appArgs.configPath, "friendsMirror.json")


def _cfg():
	try:
		with open(CFG, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return {}


def _save_cfg(d):
	try:
		with open(CFG, "w", encoding="utf-8") as f:
			json.dump(d, f)
	except Exception:
		pass


def _chat():
	"""The myChat modules, or None if myChat is not installed."""
	from . import ui as chatui, transport, uihelp
	return chatui, transport, uihelp


class FinderDialog(wx.Dialog):
	def __init__(self, engine, chatui, uihelp, users):
		super().__init__(gui.mainFrame, title=_("New chat: find users"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.engine, self.chatui, self.uihelp = engine, chatui, uihelp
		self.users = users          # list of (user, display name)
		self.shown = []
		s = wx.BoxSizer(wx.VERTICAL)
		h = guiHelper.BoxSizerHelper(self, sizer=s)
		self.q = h.addLabeledControl(_("&Search by name or user name:"), wx.TextCtrl, style=wx.TE_PROCESS_ENTER)
		self.lb = h.addLabeledControl(_("&Users:"), wx.ListBox, choices=[], size=(460, 280))
		self.vis = wx.CheckBox(self, label=_("&Show me in this list for other people"))
		self.vis.SetValue(_cfg().get("visible", True))
		h.addItem(self.vis)
		bh = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.send = bh.addButton(self, label=_("Send message &request"))
		self.refresh = bh.addButton(self, label=_("Re&fresh list"))
		bh.addButton(self, id=wx.ID_CANCEL, label=_("&Close"))
		h.addItem(bh)
		self.send.SetDefault()
		self.q.Bind(wx.EVT_TEXT, lambda e: self.fill())
		self.q.Bind(wx.EVT_TEXT_ENTER, lambda e: self.onSend(e))
		self.lb.Bind(wx.EVT_LISTBOX_DCLICK, self.onSend)
		self.send.Bind(wx.EVT_BUTTON, self.onSend)
		self.refresh.Bind(wx.EVT_BUTTON, self.onRefresh)
		self.vis.Bind(wx.EVT_CHECKBOX, self.onVis)
		self.fill()
		self.uihelp._finish(self, s)
		self.q.SetFocus()

	def fill(self):
		t = self.q.GetValue().strip().lower().lstrip("@")
		con = self.engine.d["contacts"]
		self.shown = [(u, n) for u, n in self.users if (not t or t in u or t in n.lower())]
		labels = []
		for u, n in self.shown:
			c = con.get(u)
			tag = _(" - you (chat with yourself)") if u == self.engine.me else (_(" - already a friend") if c and c.get("accepted") and not c.get("stub") else "")
			labels.append("%s (%s)%s" % (n or u, u, tag))
		self.lb.Set(labels)
		if labels:
			self.lb.SetSelection(0)

	def onVis(self, e):
		d = _cfg()
		d["visible"] = self.vis.GetValue()
		_save_cfg(d)

	def onRefresh(self, e):
		self.refresh.Disable()
		self.uihelp.speak(_("Please wait"))

		def done(users):
			try:
				self.refresh.Enable()
				if users is not None:
					me = self.engine.me
					self.users = users if me in [x[0] for x in users] else sorted(users + [(me, self.engine.d["acct"].get("name") or me)])
					self.fill()
					self.uihelp.speak(_("%d users found") % len(self.shown))
				else:
					self.uihelp.speak(_("Could not load the list. Check your internet connection."))
			except RuntimeError:
				pass
		load_users(self.engine, done)

	def target(self):
		i = self.lb.GetSelection()
		if 0 <= i < len(self.shown):
			return self.shown[i][0]
		t = self.q.GetValue().strip().lower().lstrip("@")
		return t if USER_RE.match(t) else None

	def onSend(self, e):
		u = self.target()
		if not u:
			self.uihelp.speak(_("Select a user, or type a full user name"))
			return
		con = self.engine.d["contacts"].get(u)
		if u == self.engine.me:
			self.engine.add_contact(u, lambda ok, info: None)
			self.EndModal(wx.ID_CANCEL)
			wx.CallAfter(self.chatui.controller().open_chat, self.engine.dm_chat(u))
			return
		if con and con.get("accepted") and not con.get("stub"):
			self.EndModal(wx.ID_CANCEL)
			wx.CallAfter(self.chatui.controller().open_chat, self.engine.dm_chat(u))
			return
		msg = self.uihelp.ask_text(_("Message to send with your request to %s:") % u, _("Message request"),
			_("Hi, I would like to chat with you on myChat."))
		if msg is None:
			return
		self.send.Disable()
		self.uihelp.speak(_("Sending request"))

		def done(ok, info):
			try:
				self.send.Enable()
				if not ok:
					self.uihelp.speak(info)
					wx.MessageBox(info, _("Message request"), wx.OK | wx.ICON_INFORMATION, self)
					return
				chat = self.engine.dm_chat(info)
				err = self.engine.send_text(chat, msg or _("Hi, I would like to chat with you on myChat."))
				self.uihelp.speak(err or _("Message request sent to %s") % info)
				self.fill()
			except RuntimeError:
				pass
		self.engine.add_contact(u, done)


def load_users(engine, cb):
	"""Reads the shared user index. cb(list of (user, name)) or cb(None) on error, in the main thread."""
	cm = _chat()
	if not cm:
		cb(None)
		return
	transport = cm[1]

	def work():
		found = {}
		for ev in transport.poll(INDEX_TOPIC, "12h"):
			try:
				r = json.loads(ev.get("message", ""))
				u = str(r.get("u", "")).strip().lower()
				if USER_RE.match(u):
					if r.get("off"):
						found.pop(u, None)
					else:
						found[u] = str(r.get("n") or "")[:40]
			except Exception:
				continue
		return sorted(found.items())

	def done(r):
		cb(None if isinstance(r, Exception) else r)
	engine._bg(work, done)


_shared = None


def shared():
	global _shared
	if _shared is None:
		_shared = Finder()
	return _shared


class Finder(object):
	def __init__(self):
		self.last = 0
		self.last_user = None
		self.last_vis = None
		self.timer = wx.PyTimer(self.tick)
		self.timer.Start(30000)
		wx.CallLater(10000, self.tick)

	def terminate(self):
		try:
			self.timer.Stop()
		except Exception:
			pass

	def engine(self):
		cm = _chat()
		if not cm:
			return None
		e = cm[0].controller().engine
		return e if e and e.me and e.sender else None

	def tick(self):
		"""Keep our own name in the shared user list."""
		try:
			e = self.engine()
			if not e:
				return
			vis = _cfg().get("visible", True)
			if e.me == self.last_user and vis == self.last_vis and time.time() - self.last < REPUBLISH_EVERY:
				return
			rec = {"u": e.me, "n": e.d["acct"].get("name") or e.me, "v": 1}
			if not vis:
				rec["off"] = 1
			e.sender.put(INDEX_TOPIC, json.dumps(rec), True, None)
			self.last, self.last_user, self.last_vis = time.time(), e.me, vis
		except Exception:
			pass

	def open(self):
		cm = _chat()
		if not cm:
			ui.message(_("Please install the myChat add-on first"))
			return
		chatui, transport, uihelp = cm
		ctrl = chatui.controller()
		if not ctrl.engine.me and not ctrl.sign_in():
			return
		e = ctrl.engine
		ui.message(_("Loading users"))

		def done(users):
			if users is None:
				ui.message(_("Could not load the list. Check your internet connection."))
				users = []
			self.tick()
			if e.me not in [x[0] for x in users]:
				users = sorted(users + [(e.me, e.d["acct"].get("name") or e.me)])
			uihelp._modal(FinderDialog(e, chatui, uihelp, users))
		load_users(e, done)

