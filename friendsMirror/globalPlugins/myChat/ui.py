# -*- coding: UTF-8 -*-
"""The windows of Friends Mirror: sign in, chat list, conversation, groups, status, settings."""
import os
import shutil
import tempfile
import threading
import time
import wave
import wx
import gui
import api
import globalVars
import addonHandler
from gui import guiHelper
from logHandler import log
from . import sounds
from . import extras
from . import promo
from .engine import Engine, EMOJI, DISAPPEAR, MUTE, fmt_time, fmt_size, snippet, USER_RE, EDIT_WINDOW, DELETE_WINDOW
from .uihelp import speak, _finish, _modal, ask_text, ask_form, ask_combo, choose, confirm, show_text

addonHandler.initTranslation()

VOICE_RATE = 16000
_ctrl = None


def controller():
	global _ctrl
	if _ctrl is None:
		_ctrl = Controller()
	return _ctrl


# ---------------------------------------------------------------- small reusable dialogs
def pick_many(title, prompt, labels, checked=()):
	"""A list with check boxes. Returns the list of chosen indexes, or None if cancelled."""
	dlg = wx.Dialog(gui.mainFrame, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
	s = wx.BoxSizer(wx.VERTICAL)
	h = guiHelper.BoxSizerHelper(dlg, sizer=s)
	lb = h.addLabeledControl(prompt, wx.CheckListBox, choices=labels, size=(460, 280))
	for i in checked:
		lb.Check(i, True)
	if labels:
		lb.SetSelection(0)
	h.addDialogDismissButtons(dlg.CreateButtonSizer(wx.OK | wx.CANCEL))
	_finish(dlg, s)
	lb.SetFocus()
	return _modal(dlg, lambda d, c: [i for i in range(len(labels)) if lb.IsChecked(i)] if c == wx.ID_OK else None)


def pick_contacts(engine, title, exclude=()):
	"""Choose friends with check boxes. Returns a list of user names, or None."""
	people = [c for c in engine.contacts_list() if c["u"] not in exclude and not c.get("blocked")]
	if not people:
		speak(_("You have no friends to choose from yet. Add a friend first with New chat."))
		return None
	labels = ["%s (%s)" % (engine.display_name(c["u"]), c["u"]) for c in people]
	r = pick_many(title, _("&Friends (press Space to select):"), labels)
	if r is None:
		return None
	return [people[i]["u"] for i in r]


def pick_chat(engine, title, include_broadcast=False):
	chats = [c for c in engine.chat_list("all") if c["type"] != "broadcast" or include_broadcast]
	chats = [c for c in chats if not (c["type"] == "group" and (c.get("left") or c.get("removed")))]
	if not chats:
		speak(_("There are no chats."))
		return None
	i = choose(_("&Chat:"), title, [engine.chat_title(c) for c in chats])
	return chats[i] if i >= 0 else None


def run_menu(title, options):
	"""options: list of (label, function). Shows a combo box and runs the chosen function."""
	if not options:
		return
	i = choose(_("&Choose an action:"), title, [o[0] for o in options])
	if i >= 0:
		options[i][1]()


# ---------------------------------------------------------------- the controller
class Controller(object):
	def __init__(self):
		self.engine = Engine(os.path.join(globalVars.appArgs.configPath, "Friends Mirror"), wx.CallAfter, self.on_engine)
		self.main = None
		self.windows = {}
		self.last_text = ""
		self.signing_in = False

	# ---- life cycle
	def start(self):
		try:
			self.engine.auto_login()
		except Exception:
			log.error("Friends Mirror: automatic sign in failed", exc_info=True)
		extras.start(self)

	def stop(self):
		extras.stop()
		for w in list(self.windows.values()):
			try:
				w.Destroy()
			except Exception:
				pass
		self.windows.clear()
		if self.main:
			try:
				self.main.Destroy()
			except Exception:
				pass
			self.main = None
		try:
			self.engine.shutdown()
		except Exception:
			pass

	def open(self):
		if not self.engine.me:
			if not self.sign_in():
				return
		if self.main:
			self.main.Raise()
			self.main.list.SetFocus()
			return
		if not extras.check_lock(self):
			return
		self.main = MainWindow(self)
		self.main.Show()
		self.main.Raise()
		self.main.list.SetFocus()
		sounds.play("move")

	def sign_in(self):
		if self.signing_in:
			return False
		self.signing_in = True
		try:
			dlg = LoginDialog(self)
			return _modal(dlg, lambda d, c: d.ok)
		finally:
			self.signing_in = False

	def sign_out(self):
		if not confirm(_("Sign out of Friends Mirror? You will not receive messages until you sign in again."), _("Sign out")):
			return
		for w in list(self.windows.values()):
			w.Destroy()
		self.windows.clear()
		if self.main:
			self.main.Destroy()
			self.main = None
		self.engine.logout()
		speak(_("Signed out"))

	def open_chat(self, chat):
		w = self.windows.get(chat["id"])
		if w:
			w.Raise()
			w.input.SetFocus()
			return
		w = ChatWindow(self, chat)
		self.windows[chat["id"]] = w
		w.Show()
		w.Raise()
		w.input.SetFocus()
		sounds.play("move")

	def window_gone(self, chat_id):
		self.windows.pop(chat_id, None)

	# ---- news from the engine
	def active_chat_id(self):
		for cid, w in self.windows.items():
			if w.IsActive():
				return cid
		return None

	def spoken_text(self, chat, msg):
		e = self.engine
		who = e.display_name(msg.get("f", ""))
		k = msg["kind"]
		if msg.get("deleted"):
			body = _("deleted a message")
		elif k == "voice":
			body = _("sent a voice message")
		elif k == "file":
			body = _("sent a file %s") % msg.get("at", {}).get("n", "")
		elif k == "poll":
			body = _("created a poll: %s") % msg.get("pl", {}).get("q", "")
		elif k == "contact":
			body = _("shared the contact %s") % msg.get("ct", {}).get("n", "")
		else:
			return ("%s: %s" % (who, msg.get("x", ""))) if chat["type"] == "dm" or chat["id"] in self.windows else ("%s, %s: %s" % (e.chat_title(chat), who, msg.get("x", "")))
		return "%s %s" % (who, body)

	def on_engine(self, kind, **kw):
		e = self.engine
		try:
			if kind == "message":
				self.on_message(kw["chat"], kw["msg"], kw["live"], kw["backlog"])
			elif kind in ("sysmessage", "sent", "msgupdate", "receipt"):
				chat = kw["chat"]
				w = self.windows.get(chat["id"])
				if w:
					w.refresh()
				if self.main:
					self.main.refresh()
				note = kw.get("note")
				if note and kw.get("live") and not e.is_muted(chat) and e.setting("speak") != "off":
					speak(note)
				if kind == "sysmessage" and kw.get("live") and chat["id"] in self.windows:
					speak(kw["msg"]["x"])
			elif kind == "typing":
				w = self.windows.get(kw["chat"]["id"])
				if w:
					w.update_title()
					if kw["text"] and e.setting("announce_typing"):
						speak(kw["text"])
			elif kind == "presence":
				for w in self.windows.values():
					w.update_title()
				w = next((w for w in self.windows.values() if w.chat["type"] == "dm" and w.chat.get("peer") == kw["user"]), None)
				if w and e.setting("announce_online") and e.setting("speak") != "off":
					if kw["online"]:
						sounds.play("success")
						speak(_("%s is online now") % e.display_name(kw["user"]))
					else:
						speak(_("%s went offline") % e.display_name(kw["user"]))
			elif kind == "chats":
				if self.main:
					self.main.refresh()
			elif kind == "connection":
				for w in self.windows.values():
					w.update_title()
				if self.main:
					self.main.update_title()
				speak(_("Friends Mirror is connected") if kw["connected"] else _("Friends Mirror is offline. It keeps trying to connect."))
			elif kind == "warning":
				speak(_("Warning: the security key of %s changed.") % e.display_name(kw["user"]))
				sounds.play("error")
			elif kind == "status":
				if kw.get("live") and e.setting("speak") != "off":
					speak(_("%s posted a status update") % e.display_name(kw["user"]))
			elif kind == "failed":
				sounds.play("error")
				speak(_("A message could not be sent. Open it and choose Retry."))
			elif kind == "catchup":
				n, chats = kw["unread"], kw["chats"]
				if n:
					speak(_("Friends Mirror: %d unread messages in %d chats") % (n, len(chats)))
					sounds.play("notify")
				if self.main:
					self.main.refresh()
			elif kind == "login":
				if self.main:
					self.main.update_title()
			elif kind == "error":
				log.error("Friends Mirror engine: %s" % kw.get("text"))
		except Exception:
			log.error("Friends Mirror: event %s failed" % kind, exc_info=True)

	def on_message(self, chat, msg, live, backlog):
		e = self.engine
		w = self.windows.get(chat["id"])
		if w:
			w.refresh()
		if self.main:
			self.main.refresh()
		if backlog or msg.get("own"):
			return
		if msg["kind"] == "sys":
			if live and (w or chat["type"] == "group"):
				speak(msg["x"])
			return
		if extras.on_incoming(self, chat, msg, live):
			return
		if e.is_muted(chat) or e.setting("speak") == "off" and not e.setting("sound"):
			return
		self.notify(chat, msg)
		mode = e.setting("speak")
		if mode == "off":
			return
		if chat.get("request"):
			text = _("Message request from %s") % e.display_name(msg["f"])
		elif mode == "sender":
			text = _("New message from %s") % e.display_name(msg["f"])
		else:
			text = self.spoken_text(chat, msg)
		self.last_text = text
		speak(text)

	def notify(self, chat, msg):
		"""Show a Friends Mirror notification (who sent what) and play the message tone."""
		e = self.engine
		try:
			if e.setting("tone") and e.setting("sound"):
				sounds.play("notify", force=True)
			if e.setting("announce_new"):
				speak(_("New notification from Friends Mirror"))
			if not e.setting("popup"):
				return
			who = e.display_name(msg.get("f", ""))
			if chat.get("request"):
				body = _("Message request from %s") % who
			elif e.setting("popup_preview"):
				body = self.spoken_text(chat, msg)
			else:
				body = _("New message from %s") % who
			if chat["type"] != "dm" and not chat.get("request"):
				body = "%s: %s" % (e.chat_title(chat), body)
			from . import toast
			toast.show(_("Friends Mirror - %s") % who, body[:250])
		except Exception:
			log.error("Friends Mirror: notification failed", exc_info=True)

	# ---- gestures
	def repeat_last(self):
		speak(self.last_text or _("No new message yet"))

	def say_unread(self):
		if not self.engine.me:
			speak(_("You are not signed in to Friends Mirror"))
			return
		n = self.engine.unread_total()
		speak(_("%d unread messages") % n if n else _("No unread messages"))

	# ---- actions used by several windows
	def start_chat_with(self, text):
		e = self.engine
		text = text.strip()
		if not text:
			return
		if text.endswith(")") and "(" in text:
			text = text[text.rfind("(") + 1:-1]
		u = text.strip().lstrip("@").lower()
		con = e.d["contacts"].get(u)
		if con and con.get("accepted") and not con.get("stub"):
			self.open_chat(e.dm_chat(u))
			return
		speak(_("Please wait"))
		sounds.play("working")

		def done(ok, info):
			if ok:
				sounds.play("success")
				self.open_chat(e.dm_chat(info))
			else:
				sounds.play("error")
				wx.MessageBox(info, _("New chat"), wx.OK | wx.ICON_INFORMATION, gui.mainFrame)
		e.add_contact(u, done)

	def new_chat(self):
		from . import finder
		finder.shared().open()

	def new_group(self):
		e = self.engine
		r = ask_form(_("New group"), [
			{"key": "name", "label": _("Group &name:"), "required": True},
			{"key": "about", "label": _("&Description (optional):")}])
		if not r:
			return
		members = pick_contacts(e, _("Add friends to the group"))
		if members is None:
			return
		chat, err = e.create_group(r["name"], r["about"], members)
		if err:
			wx.MessageBox(err, _("New group"), wx.OK | wx.ICON_WARNING, gui.mainFrame)
			return
		sounds.play("success")
		self.open_chat(chat)

	def new_broadcast(self):
		e = self.engine
		r = ask_form(_("New broadcast list"), [{"key": "name", "label": _("List &name:"), "required": True}],
			intro=_("A broadcast list sends your message to each friend separately. They reply to you privately."))
		if not r:
			return
		members = pick_contacts(e, _("Choose the friends for this list"))
		if members is None:
			return
		chat, err = e.create_broadcast(r["name"], members)
		if err:
			wx.MessageBox(err, _("New broadcast list"), wx.OK | wx.ICON_WARNING, gui.mainFrame)
			return
		self.open_chat(chat)

	def show_status(self):
		StatusDialog(self).run()

	def show_starred(self):
		e = self.engine
		items = e.starred()
		if not items:
			speak(_("You have no starred messages."))
			return
		ResultList(self, _("Starred messages"), [(("%s: %s" % (e.chat_title(c), e.format_message(c, m))), c, m) for c, m in items]).run()

	def search_everywhere(self):
		e = self.engine
		q = ask_text(_("&Search for:"), _("Search all chats"))
		if not q:
			return
		hits = e.search_all(q)
		if not hits:
			speak(_("Nothing found"))
			return
		ResultList(self, _("Search results"), [(("%s: %s" % (e.chat_title(c), e.format_message(c, m))), c, m) for c, m in hits]).run()

	def settings(self):
		run_menu(_("Friends Mirror settings"), [
			(_("Profile, privacy and notifications"), self.edit_settings)] + extras.settings_items(self) + [
			(_("Blocked contacts"), self.blocked_contacts),
			(_("About Friends Mirror and how to use it"), lambda: show_text(_("About Friends Mirror"), about_text(self.engine))),
		])

	def edit_settings(self):
		e = self.engine
		speak_modes = [_("Read the full message"), _("Read only who wrote"), _("Do not read messages")]
		keys = ["full", "sender", "off"]
		dis = [d[1] for d in DISAPPEAR]
		r = ask_form(_("Profile and privacy"), [
			{"key": "name", "label": _("Your &name:"), "default": e.d["acct"]["name"]},
			{"key": "about", "label": _("&About:"), "default": e.d["acct"]["about"]},
			{"key": "speak", "label": _("&When a message arrives:"), "type": "combo", "choices": speak_modes,
				"default": speak_modes[keys.index(e.setting("speak"))]},
			{"key": "sound", "label": _("Play &sounds"), "type": "check", "default": e.setting("sound")},
			{"key": "tone", "label": _("Play a message &tone when a message arrives"), "type": "check", "default": e.setting("tone")},
			{"key": "annnew", "label": _("Say &New notification from Friends Mirror wherever I am"), "type": "check", "default": e.setting("announce_new")},
			{"key": "promo", "label": _("Show a Friends Mirror &reminder notification every minute"), "type": "check", "default": promo.enabled()},
			{"key": "popup", "label": _("Show a &notification from Friends Mirror when a message arrives"), "type": "check", "default": e.setting("popup")},
			{"key": "prev", "label": _("Show the message &text in the notification"), "type": "check", "default": e.setting("popup_preview")},
			{"key": "autodl", "label": _("Download received &files automatically"), "type": "check", "default": e.setting("autodl")},
			{"key": "online", "label": _("Share my &online status and last seen"), "type": "check", "default": e.setting("show_online")},
			{"key": "rr", "label": _("Send &read receipts"), "type": "check", "default": e.setting("read_receipts")},
			{"key": "typing", "label": _("Show when I am &typing"), "type": "check", "default": e.setting("typing")},
			{"key": "aonl", "label": _("Say when the friend whose chat is open comes online or goes offline"), "type": "check", "default": e.setting("announce_online")},
			{"key": "atyp", "label": _("Say when a friend is typing"), "type": "check", "default": e.setting("announce_typing")},
			{"key": "dd", "label": _("&Disappearing messages for new chats:"), "type": "combo", "choices": dis,
				"default": dict(DISAPPEAR).get(e.setting("default_disap") or 0)},
		])
		if not r:
			return
		e.set_profile(r["name"], r["about"])
		e.set_setting("speak", keys[r["speak_index"]])
		e.set_setting("sound", r["sound"])
		sounds.set_enabled(r["sound"])
		e.set_setting("tone", r["tone"])
		e.set_setting("popup", r["popup"])
		promo.set_enabled(r["promo"])
		e.set_setting("announce_new", r["annnew"])
		e.set_setting("popup_preview", r["prev"])
		e.set_setting("autodl", r["autodl"])
		e.set_setting("show_online", r["online"])
		e.set_setting("read_receipts", r["rr"])
		e.set_setting("typing", r["typing"])
		e.set_setting("announce_typing", r["atyp"])
		e.set_setting("announce_online", r["aonl"])
		e.set_setting("default_disap", DISAPPEAR[r["dd_index"]][0])
		speak(_("Settings saved"))

	def blocked_contacts(self):
		e = self.engine
		users = e.blocked_list()
		if not users:
			speak(_("You have not blocked anybody."))
			return
		i = choose(_("Choose a contact to unblock:"), _("Blocked contacts"), [e.display_name(u) + " (" + u + ")" for u in users])
		if i >= 0:
			e.block(users[i], False)
			speak(_("Unblocked %s") % e.display_name(users[i]))


# ---------------------------------------------------------------- sign in
class LoginDialog(wx.Dialog):
	def __init__(self, ctrl):
		super().__init__(gui.mainFrame, title=_("Friends Mirror: sign in or create an account"), style=wx.DEFAULT_DIALOG_STYLE)
		self.ctrl = ctrl
		self.ok = False
		self.busy = False
		s = wx.BoxSizer(wx.VERTICAL)
		h = guiHelper.BoxSizerHelper(self, sizer=s)
		h.addItem(wx.StaticText(self, label=_(
			"New here? Choose a user name and a password and press Sign in: your account is created for you.\n"
			"Your friends find you by your user name. Remember your password: it also protects your messages.")))
		self.user = h.addLabeledControl(_("&User name:"), wx.TextCtrl, value=ctrl.engine.last_username())
		self.pw = h.addLabeledControl(_("&Password:"), wx.TextCtrl, style=wx.TE_PASSWORD | wx.TE_PROCESS_ENTER)
		self.remember = wx.CheckBox(self, label=_("&Stay signed in on this computer"))
		self.remember.SetValue(True)
		h.addItem(self.remember)
		bh = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.go = bh.addButton(self, id=wx.ID_OK, label=_("&Sign in"))
		bh.addButton(self, id=wx.ID_CANCEL, label=_("&Cancel"))
		h.addItem(bh)
		self.go.SetDefault()
		self.go.Bind(wx.EVT_BUTTON, self.onGo)
		self.pw.Bind(wx.EVT_TEXT_ENTER, self.onGo)
		_finish(self, s)
		(self.pw if self.user.GetValue() else self.user).SetFocus()

	def onGo(self, e):
		if self.busy:
			return
		u, p = self.user.GetValue().strip(), self.pw.GetValue()
		if not u or not p:
			speak(_("Please type your user name and password"))
			return
		self.busy = True
		self.go.Disable()
		speak(_("Please wait"))
		sounds.play("working")

		def done(ok, text):
			try:
				self.busy = False
				self._done(ok, text)
			except RuntimeError:
				pass
		self.ctrl.engine.login(u, p, self.remember.GetValue(), done)

	def _done(self, ok, text):
		if ok:
			self.ok = True
			sounds.play("success")
			self.EndModal(wx.ID_OK)
			return
		self.go.Enable()
		sounds.play("error")
		speak(text)
		wx.MessageBox(text, _("Friends Mirror"), wx.OK | wx.ICON_WARNING, self)
		self.pw.SetFocus()


# ---------------------------------------------------------------- chat list
class MainWindow(wx.Dialog):
	MODES = [("all", _("All chats")), ("unread", _("Unread chats")), ("groups", _("Groups")),
		("archived", _("Archived chats")), ("requests", _("Message requests"))]

	def __init__(self, ctrl):
		super().__init__(gui.mainFrame, title="Friends Mirror", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.ctrl = ctrl
		self.e = ctrl.engine
		self.chats = []
		s = wx.BoxSizer(wx.VERTICAL)
		h = guiHelper.BoxSizerHelper(self, sizer=s)
		self.mode = h.addLabeledControl(_("&Show:"), wx.Choice, choices=[m[1] for m in self.MODES])
		self.mode.SetSelection(0)
		self.list = h.addLabeledControl(_("&Chats (Enter to open):"), wx.ListBox, choices=[], size=(680, 320))
		b1 = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.openBtn = b1.addButton(self, label=_("&Open chat"))
		self.newBtn = b1.addButton(self, label=_("&New chat"))
		self.groupBtn = b1.addButton(self, label=_("New &group"))
		self.bcBtn = b1.addButton(self, label=_("New &broadcast list"))
		self.optBtn = b1.addButton(self, label=_("Chat &options"))
		h.addItem(b1)
		b2 = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.statusBtn = b2.addButton(self, label=_("S&tatus"))
		self.starBtn = b2.addButton(self, label=_("Starre&d messages"))
		self.searchBtn = b2.addButton(self, label=_("Sear&ch"))
		self.aiBtn = b2.addButton(self, label=_("A&I assistant"))
		self.moreBtn = b2.addButton(self, label=_("&More tools"))
		self.setBtn = b2.addButton(self, label=_("Se&ttings"))
		self.outBtn = b2.addButton(self, label=_("Sign o&ut"))
		self.closeBtn = b2.addButton(self, id=wx.ID_CLOSE, label=_("C&lose"))
		h.addItem(b2)
		self.openBtn.Bind(wx.EVT_BUTTON, lambda ev: self.open_selected())
		self.newBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.new_chat())
		self.groupBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.new_group())
		self.bcBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.new_broadcast())
		self.optBtn.Bind(wx.EVT_BUTTON, lambda ev: self.options())
		self.statusBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.show_status())
		self.starBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.show_starred())
		self.searchBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.search_everywhere())
		self.aiBtn.Bind(wx.EVT_BUTTON, lambda ev: extras.ai_assistant(ctrl))
		self.moreBtn.Bind(wx.EVT_BUTTON, lambda ev: extras.main_tools(ctrl))
		self.setBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.settings())
		self.outBtn.Bind(wx.EVT_BUTTON, lambda ev: ctrl.sign_out())
		self.closeBtn.Bind(wx.EVT_BUTTON, lambda ev: self.Close())
		self.mode.Bind(wx.EVT_CHOICE, lambda ev: self.refresh())
		self.list.Bind(wx.EVT_KEY_DOWN, self.onKey)
		self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda ev: self.open_selected())
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.SetEscapeId(wx.ID_CLOSE)
		_finish(self, s)
		self.refresh()
		self.update_title()

	def update_title(self):
		e = self.e
		state = "" if e.connected else " - " + _("connecting")
		n = e.unread_total()
		self.SetTitle("Friends Mirror - %s%s%s" % (e.me or "", _(", %d unread") % n if n else "", state))

	def line(self, c):
		e = self.e
		bits = [e.chat_title(c)]
		if c["type"] == "group":
			bits[0] = _("Group %s") % bits[0]
		elif c["type"] == "broadcast":
			bits[0] = _("Broadcast list %s") % bits[0]
		if c.get("pinned"):
			bits.append(_("pinned"))
		if e.is_muted(c):
			bits.append(_("muted"))
		if c.get("unread"):
			bits.append(_("%d unread") % c["unread"])
		if c["type"] == "dm" and e.is_online(c["peer"]):
			bits.append(_("online"))
		prev = e.preview(c)
		if prev:
			bits.append(prev)
		if c["msgs"]:
			bits.append(fmt_time(c["last_ts"]))
		return ", ".join(bits)

	def refresh(self):
		sel = None
		i = self.list.GetSelection()
		if i != wx.NOT_FOUND and i < len(self.chats):
			sel = self.chats[i]["id"]
		self.chats = self.e.chat_list(self.MODES[max(0, self.mode.GetSelection())][0])
		self.list.Set([self.line(c) for c in self.chats])
		idx = 0
		for n, c in enumerate(self.chats):
			if c["id"] == sel:
				idx = n
		if self.chats:
			self.list.SetSelection(idx)
		self.update_title()

	def current(self):
		i = self.list.GetSelection()
		if i == wx.NOT_FOUND or i >= len(self.chats):
			speak(_("No chat selected"))
			return None
		return self.chats[i]

	def open_selected(self):
		c = self.current()
		if c:
			self.ctrl.open_chat(c)

	def options(self):
		c = self.current()
		if c:
			chat_options(self.ctrl, c, None)
			self.refresh()

	def onKey(self, ev):
		k = ev.GetKeyCode()
		if k in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			self.open_selected()
		elif k == wx.WXK_DELETE:
			c = self.current()
			if c and confirm(_("Delete the chat %s?") % self.e.chat_title(c), _("Delete chat")):
				self.e.delete_chat(c)
				w = self.ctrl.windows.get(c["id"])
				if w:
					w.Destroy()
				self.refresh()
		else:
			ev.Skip()

	def onClose(self, ev):
		self.ctrl.main = None
		self.Destroy()


# ---------------------------------------------------------------- options shared by the list and the chat window
def chat_options(ctrl, chat, window):
	e = ctrl.engine
	opts = []
	name = e.chat_title(chat)

	def refresh():
		if window:
			window.refresh()
			window.update_title()
		if ctrl.main:
			ctrl.main.refresh()

	if chat["type"] == "dm" and chat.get("request"):
		opts += [(_("Accept this chat"), lambda: (e.accept_request(chat), speak(_("Accepted")), refresh())),
			(_("Block this person"), lambda: (e.block(chat["peer"], True), speak(_("Blocked")), refresh())),
			(_("Delete this request"), lambda: (e.delete_chat(chat), window and window.Destroy(), refresh()))]
	if window:
		opts.append((_("Search in this chat"), window.search))
	if chat["type"] == "dm":
		u = chat["peer"]
		c = e.d["contacts"].get(u, {})
		opts.append((_("Contact info and safety number"), lambda: show_text(_("Contact info"), e.contact_info(u))))
		opts.append((_("Change the name I use for this contact"), lambda: rename(u)))
		if c.get("new_pk"):
			opts.append((_("Trust the new security key"), lambda: (e.trust_new_key(u), refresh())))
		if c.get("blocked"):
			opts.append((_("Unblock this contact"), lambda: (e.block(u, False), speak(_("Unblocked")), refresh())))
		elif not chat.get("request"):
			opts.append((_("Block this contact"), lambda: (e.block(u, True), speak(_("Blocked")), refresh())))
	elif chat["type"] == "group":
		opts.append((_("Group info"), lambda: show_text(_("Group info"), e.group_info(chat))))
		if not (chat.get("left") or chat.get("removed")):
			opts.append((_("Add members"), lambda: group_add(ctrl, chat)))
			opts.append((_("Remove a member"), lambda: group_remove(ctrl, chat)))
			opts.append((_("Make or remove an admin"), lambda: group_admin(ctrl, chat)))
			opts.append((_("Change group name and description"), lambda: group_edit(ctrl, chat)))
			opts.append((_("Leave the group"), lambda: leave(chat)))
	elif chat["type"] == "broadcast":
		opts.append((_("Show the people in this list"), lambda: show_text(name, "\n".join(e.display_name(u) for u in chat["members"]))))
		opts.append((_("Change the people in this list"), lambda: broadcast_edit(ctrl, chat)))
	opts.append((_("Unmute notifications") if e.is_muted(chat) else _("Mute notifications"), lambda: mute(chat)))
	opts.append((_("Unpin this chat") if chat.get("pinned") else _("Pin this chat"), lambda: pin(chat)))
	opts.append((_("Unarchive this chat") if chat.get("archived") else _("Archive this chat"), lambda: archive(chat)))
	if chat["type"] != "broadcast":
		opts.append((_("Disappearing messages"), lambda: disappearing(chat)))
	opts.append((_("Mark as unread"), lambda: (e.mark_unread(chat), speak(_("Marked as unread")), refresh())))
	opts.append((_("Export this chat to a text file"), lambda: export(chat)))
	opts.append((_("Clear all messages"), lambda: clear(chat)))
	opts.append((_("Delete this chat"), lambda: delete(chat)))

	def rename(u):
		n = ask_text(_("Name for this contact (leave empty to use their own name):"), _("Contact name"),
			e.d["contacts"][u].get("nick", ""))
		if n is not None:
			e.rename_contact(u, n)
			refresh()

	def mute(c):
		if e.is_muted(c):
			e.mute(c, 0)
			speak(_("Unmuted"))
		else:
			i = choose(_("Mute for:"), _("Mute notifications"), [m[1] for m in MUTE])
			if i < 0:
				return
			e.mute(c, MUTE[i][0])
			speak(_("Muted"))
		refresh()

	def pin(c):
		err = e.pin(c, not c.get("pinned"))
		speak(err or (_("Pinned") if c.get("pinned") else _("Unpinned")))
		refresh()

	def archive(c):
		e.archive(c, not c.get("archived"))
		speak(_("Archived") if c.get("archived") else _("Unarchived"))
		refresh()

	def disappearing(c):
		labels = [d[1] for d in DISAPPEAR]
		i = choose(_("Make new messages disappear after:"), _("Disappearing messages"), labels)
		if i < 0:
			return
		err = e.set_disappearing(c, DISAPPEAR[i][0])
		if err:
			wx.MessageBox(err, _("Disappearing messages"), wx.OK | wx.ICON_WARNING, gui.mainFrame)
		refresh()

	def export(c):
		dlg = wx.FileDialog(gui.mainFrame, _("Save the chat as"), defaultFile="%s.txt" % snippet(name, 30).replace("/", "_"),
			wildcard="Text (*.txt)|*.txt", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
		path = _modal(dlg, lambda d, code: d.GetPath() if code == wx.ID_OK else None)
		if path:
			with open(path, "w", encoding="utf-8") as f:
				f.write(e.export_chat(c))
			speak(_("Chat saved"))

	def clear(c):
		if confirm(_("Clear all messages of %s on this computer?") % name, _("Clear chat")):
			e.clear_chat(c)
			refresh()

	def delete(c):
		if confirm(_("Delete the chat %s? Messages are removed from this computer.") % name, _("Delete chat")):
			e.delete_chat(c)
			if window:
				window.Destroy()
			if ctrl.main:
				ctrl.main.refresh()

	def leave(c):
		if confirm(_("Leave the group %s?") % name, _("Leave group")):
			e.leave_group(c)
			refresh()

	run_menu(_("Options for %s") % name, opts)


def group_add(ctrl, chat):
	e = ctrl.engine
	users = pick_contacts(e, _("Add members"), exclude=chat["members"])
	if users:
		err = e.group_add(chat, users)
		speak(err or _("Members added"))


def group_remove(ctrl, chat):
	e = ctrl.engine
	others = [u for u in chat["members"] if u != e.me]
	if not others:
		speak(_("There is nobody else in the group."))
		return
	i = choose(_("Member to remove:"), _("Remove a member"), [e.display_name(u) for u in others])
	if i >= 0:
		err = e.group_remove(chat, others[i])
		speak(err or _("Member removed"))


def group_admin(ctrl, chat):
	e = ctrl.engine
	others = [u for u in chat["members"] if u != e.me]
	if not others:
		return
	labels = [e.display_name(u) + (_(" (admin)") if u in chat["admins"] else "") for u in others]
	i = choose(_("Member:"), _("Make or remove an admin"), labels)
	if i >= 0:
		err = e.group_set_admin(chat, others[i], others[i] not in chat["admins"])
		speak(err or _("Done"))


def group_edit(ctrl, chat):
	e = ctrl.engine
	r = ask_form(_("Group details"), [
		{"key": "name", "label": _("Group &name:"), "default": chat["name"], "required": True},
		{"key": "about", "label": _("&Description:"), "default": chat.get("about", "")}])
	if r:
		err = e.group_edit(chat, r["name"], r["about"])
		speak(err or _("Saved"))


def broadcast_edit(ctrl, chat):
	e = ctrl.engine
	people = [c for c in e.contacts_list() if not c.get("blocked")]
	labels = ["%s (%s)" % (e.display_name(c["u"]), c["u"]) for c in people]
	checked = [i for i, c in enumerate(people) if c["u"] in chat["members"]]
	r = pick_many(_("People in this list"), _("&Friends (press Space to select):"), labels, checked)
	if r is not None:
		chat["members"] = [people[i]["u"] for i in r]
		e.touch()
		speak(_("Saved"))


# ---------------------------------------------------------------- the conversation window
class ChatWindow(wx.Dialog):
	def __init__(self, ctrl, chat):
		super().__init__(gui.mainFrame, title=ctrl.engine.chat_title(chat), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.ctrl = ctrl
		self.e = ctrl.engine
		self.chat = chat
		self.reply = None
		self.limit = 300
		self.shown = []
		self.recorder = None
		s = wx.BoxSizer(wx.VERTICAL)
		h = guiHelper.BoxSizerHelper(self, sizer=s)
		self.list = h.addLabeledControl(_("&Messages (Enter for message actions):"), wx.ListBox, choices=[], size=(720, 340))
		self.input = h.addLabeledControl(_("Your &message (Enter to send):"), wx.TextCtrl, style=wx.TE_PROCESS_ENTER)
		b1 = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.sendBtn = b1.addButton(self, label=_("&Send"))
		self.attachBtn = b1.addButton(self, label=_("&Attach"))
		self.voiceBtn = b1.addButton(self, label=_("&Record voice message"))
		self.discardBtn = b1.addButton(self, label=_("&Discard recording"))
		self.discardBtn.Disable()
		h.addItem(b1)
		b2 = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.msgBtn = b2.addButton(self, label=_("Message ac&tions"))
		self.optBtn = b2.addButton(self, label=_("Chat &options"))
		self.aiBtn = b2.addButton(self, label=_("A&I and chat tools"))
		self.moreBtn = b2.addButton(self, label=_("Show &earlier messages"))
		self.closeBtn = b2.addButton(self, id=wx.ID_CLOSE, label=_("C&lose"))
		h.addItem(b2)
		self.sendBtn.Bind(wx.EVT_BUTTON, self.onSend)
		self.input.Bind(wx.EVT_TEXT_ENTER, self.onSend)
		self.input.Bind(wx.EVT_TEXT, self.onText)
		self.attachBtn.Bind(wx.EVT_BUTTON, lambda ev: self.attach())
		self.voiceBtn.Bind(wx.EVT_BUTTON, self.onVoice)
		self.discardBtn.Bind(wx.EVT_BUTTON, lambda ev: self.stop_recording(send=False))
		self.msgBtn.Bind(wx.EVT_BUTTON, lambda ev: self.message_actions())
		self.optBtn.Bind(wx.EVT_BUTTON, lambda ev: chat_options(ctrl, self.chat, self))
		self.aiBtn.Bind(wx.EVT_BUTTON, lambda ev: extras.chat_tools(ctrl, self.chat, self))
		self.moreBtn.Bind(wx.EVT_BUTTON, self.onMore)
		self.closeBtn.Bind(wx.EVT_BUTTON, lambda ev: self.Close())
		self.list.Bind(wx.EVT_KEY_DOWN, self.onListKey)
		self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda ev: self.message_actions())
		self.Bind(wx.EVT_CHAR_HOOK, self.onChar)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.SetEscapeId(wx.ID_CLOSE)
		_finish(self, s)
		if chat.get("draft"):
			self.input.SetValue(chat["draft"])
			self.input.SetInsertionPointEnd()
		self.e.set_viewing(chat, True)
		self.refresh(select_last=True)
		self.update_title()
		if chat["type"] == "dm" and chat.get("request"):
			wx.CallAfter(self.ask_request)

	def ask_request(self):
		name = self.e.chat_title(self.chat)
		i = choose(_("%s is not in your friends list. What do you want to do?") % name, _("Message request"),
			[_("Accept and chat"), _("Block this person"), _("Delete this request"), _("Decide later")])
		if i == 0:
			self.e.accept_request(self.chat)
			self.update_title()
		elif i == 1:
			self.e.block(self.chat["peer"], True)
			self.Close()
		elif i == 2:
			self.e.delete_chat(self.chat)
			self.Destroy()
			self.ctrl.window_gone(self.chat["id"])
			if self.ctrl.main:
				self.ctrl.main.refresh()

	def update_title(self):
		e, c = self.e, self.chat
		title = e.chat_title(c)
		if c["type"] == "dm":
			t = e.typing_text(c) or e.presence_text(c["peer"])
			if t:
				title += " - " + t
		elif c["type"] == "group":
			t = e.typing_text(c)
			title += " - " + (t or _("%d members") % len(c["members"]))
		if not e.connected:
			title += " (" + _("offline") + ")"
		if e.is_muted(c):
			title += " " + _("(muted)")
		self.SetTitle(title)

	def refresh(self, select_last=False):
		msgs = self.chat["msgs"][-self.limit:]
		sel = None
		i = self.list.GetSelection()
		if i != wx.NOT_FOUND and i < len(self.shown):
			sel = self.shown[i]["id"]
		at_end = select_last or i == wx.NOT_FOUND or i >= len(self.shown) - 1
		self.shown = msgs
		self.list.Set([self.e.format_message(self.chat, m) for m in msgs])
		self.moreBtn.Enable(len(self.chat["msgs"]) > len(msgs))
		if not msgs:
			return
		idx = len(msgs) - 1
		if not at_end:
			for n, m in enumerate(msgs):
				if m["id"] == sel:
					idx = n
		self.list.SetSelection(idx)
		if self.IsActive() or select_last:
			self.e.mark_read(self.chat)

	def onMore(self, ev):
		self.limit += 300
		self.refresh()
		self.list.SetFocus()

	def current_msg(self):
		i = self.list.GetSelection()
		if i == wx.NOT_FOUND or i >= len(self.shown):
			speak(_("No message selected"))
			return None
		return self.shown[i]

	# ---- sending
	def onText(self, ev):
		self.chat["draft"] = self.input.GetValue()
		if self.input.GetValue():
			self.e.typing_ping(self.chat)
		ev.Skip()

	def onSend(self, ev):
		text = self.input.GetValue().strip()
		if not text:
			return
		err = self.e.send_text(self.chat, text, reply=self.reply)
		if err:
			sounds.play("error")
			speak(err)
			return
		self.input.SetValue("")
		self.chat["draft"] = ""
		self.reply = None
		sounds.play("success")
		self.input.SetFocus()

	def onChar(self, ev):
		if ev.GetKeyCode() == wx.WXK_F5 and self.chat["type"] == "dm":
			speak(self.e.presence_detail(self.chat["peer"]))
			return
		if ev.GetKeyCode() == wx.WXK_ESCAPE:
			if self.recorder:
				self.stop_recording(send=False)
				return
			if self.reply:
				self.reply = None
				speak(_("Reply cancelled"))
				return
		ev.Skip()

	def start_reply(self, msg):
		self.reply = msg
		self.input.SetFocus()
		speak(_("Replying to %s: %s") % (self.e.display_name(msg.get("f", "")), snippet(self.e.msg_summary(self.chat, msg).split(": ", 1)[-1], 80)))

	# ---- attachments
	def attach(self):
		e = self.e
		i = choose(_("&Attach:"), _("Attach"), [_("A file (document, picture, audio)"), _("A poll"), _("A contact")])
		if i == 0:
			dlg = wx.FileDialog(gui.mainFrame, _("Choose a file to send"), style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
			path = _modal(dlg, lambda d, c: d.GetPath() if c == wx.ID_OK else None)
			if path:
				self.send_file(path)
		elif i == 1:
			self.make_poll()
		elif i == 2:
			people = [c for c in e.contacts_list() if not c.get("blocked")]
			if not people:
				speak(_("You have no friends yet."))
				return
			j = choose(_("&Contact to share:"), _("Share a contact"), ["%s (%s)" % (e.display_name(c["u"]), c["u"]) for c in people])
			if j >= 0:
				e.share_contact(self.chat, people[j]["u"])

	def send_file(self, path, kind="file", duration=0):
		speak(_("Sending file, please wait"))
		sounds.play("working")

		def done(err):
			if err:
				sounds.play("error")
				speak(err)
				wx.MessageBox(err, _("Send file"), wx.OK | wx.ICON_WARNING, self)
			else:
				sounds.play("success")
				speak(_("Sent"))
		self.e.send_file(self.chat, path, kind, duration, done)

	def make_poll(self):
		fields = [{"key": "q", "label": _("&Question:"), "required": True}]
		for n in range(1, 7):
			fields.append({"key": "o%d" % n, "label": _("Option &%d:") % n})
		fields.append({"key": "multi", "label": _("Allow &more than one answer"), "type": "check"})
		r = ask_form(_("Create a poll"), fields, intro=_("Fill in at least two options."))
		if not r:
			return
		opts = [r["o%d" % n] for n in range(1, 7) if r["o%d" % n]]
		err = self.e.send_poll(self.chat, r["q"], opts, r["multi"])
		if err:
			speak(err)
			wx.MessageBox(err, _("Poll"), wx.OK | wx.ICON_WARNING, self)

	# ---- voice messages
	def onVoice(self, ev):
		if self.recorder:
			self.stop_recording(send=True)
			return
		err = self.e._can_send(self.chat)
		if err:
			speak(err)
			return
		try:
			from .winaudio import Microphone
			self.frames = []
			self.recorder = Microphone(self.frames.append, rate=VOICE_RATE)
			self.recorder.start()
		except Exception as ex:
			self.recorder = None
			speak(str(ex))
			wx.MessageBox(str(ex), _("Voice message"), wx.OK | wx.ICON_WARNING, self)
			return
		self.rec_start = time.time()
		self.voiceBtn.SetLabel(_("&Stop and send voice message"))
		self.discardBtn.Enable()
		sounds.play("working")
		speak(_("Recording. Press the button again to send, or Escape to discard."))

	def stop_recording(self, send):
		rec, self.recorder = self.recorder, None
		if not rec:
			return
		try:
			rec.stop()
		except Exception:
			pass
		self.voiceBtn.SetLabel(_("&Record voice message"))
		self.discardBtn.Disable()
		secs = int(time.time() - self.rec_start)
		if not send or not self.frames or secs < 1:
			speak(_("Recording discarded") if not send else _("The recording was too short"))
			return
		path = os.path.join(self.e.files_dir(), "voice_%d.wav" % int(time.time()))
		with wave.open(path, "wb") as w:
			w.setnchannels(1)
			w.setsampwidth(2)
			w.setframerate(VOICE_RATE)
			w.writeframes(b"".join(self.frames))
		self.send_file(path, "voice", secs)
		self.voiceBtn.SetFocus()

	# ---- actions on one message
	def onListKey(self, ev):
		k = ev.GetKeyCode()
		if k in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			m = self.current_msg()
			if m and m["kind"] == "voice" and not m.get("deleted") and not ev.ShiftDown():
				self.play_voice(m)
			else:
				self.message_actions()
		elif k == wx.WXK_DELETE:
			m = self.current_msg()
			if m and confirm(_("Delete this message for you?"), _("Delete message")):
				self.e.delete_for_me(self.chat, m)
				self.refresh()
		elif k == ord("C") and ev.ControlDown():
			m = self.current_msg()
			if m:
				api.copyToClip(self.list.GetString(self.list.GetSelection()))
				speak(_("Copied"))
		else:
			ev.Skip()

	def play_voice(self, m):
		"""Enter on a voice message plays it (downloading first if needed). Shift+Enter opens the actions."""
		at = m.get("at", {})
		if at.get("path") and os.path.isfile(at["path"]):
			sounds.play_file(at["path"])
		else:
			self.download(m, play=True)

	def message_actions(self):
		m = self.current_msg()
		if not m or m["kind"] == "sys":
			return
		e, chat = self.e, self.chat
		opts = []
		deleted = m.get("deleted")
		bcast = chat["type"] == "broadcast"
		if not deleted:
			if not bcast:
				opts.append((_("Reply"), lambda: self.start_reply(m)))
			opts.append((_("Forward"), lambda: self.forward(m)))
			opts.append((_("Copy"), lambda: self.copy(m)))
			if not bcast:
				opts.append((_("React with an emoji"), lambda: self.react(m)))
			opts.append((_("Remove star") if m.get("star") else _("Star this message"), lambda: (e.star(m), self.refresh())))
			opts.append((_("Message info"), lambda: show_text(_("Message info"), e.message_info(chat, m))))
			if m.get("own") and m["kind"] == "text" and not bcast and time.time() - m["ts"] <= EDIT_WINDOW:
				opts.append((_("Edit"), lambda: self.edit(m)))
			if m["kind"] in ("file", "voice"):
				opts += self.file_options(m)
			if m["kind"] == "poll":
				opts.append((_("Vote in this poll"), lambda: self.vote(m)))
			if m["kind"] == "contact":
				u = m.get("ct", {}).get("u", "")
				if u and u != e.me:
					opts.append((_("Chat with %s") % m["ct"].get("n", u), lambda: self.ctrl.start_chat_with(u)))
		if m.get("own") and m.get("sent") == -1:
			opts.insert(0, (_("Retry sending"), lambda: (e.retry(chat, m), self.refresh())))
		opts += extras.message_options(self.ctrl, chat, self, m)
		opts.append((_("Delete for me"), lambda: self.delete_me(m)))
		if m.get("own") and not deleted and not bcast and time.time() - m["ts"] <= DELETE_WINDOW:
			opts.append((_("Delete for everyone"), lambda: self.delete_all(m)))
		run_menu(_("Message actions"), opts)

	def file_options(self, m):
		at = m.get("at", {})
		opts = []
		have = at.get("path") and os.path.isfile(at["path"])
		if not have:
			opts.append((_("Download"), lambda: self.download(m, play=False)))
		else:
			if m["kind"] == "voice":
				opts.append((_("Play voice message"), lambda: sounds.play_file(at["path"])))
			else:
				opts.append((_("Open"), lambda: self.open_file(at["path"])))
			opts.append((_("Save a copy as"), lambda: self.save_copy(m)))
		if m["kind"] == "voice" and not have:
			opts.append((_("Download and play"), lambda: self.download(m, play=True)))
		return opts

	def download(self, m, play):
		speak(_("Downloading"))

		def done(err):
			if err:
				speak(err)
			else:
				speak(_("Downloaded"))
				if play and m["kind"] == "voice":
					sounds.play_file(m["at"]["path"])
		self.e.download_attachment(self.chat, m, done)

	def open_file(self, path):
		try:
			os.startfile(path)
		except Exception as ex:
			speak(str(ex))

	def save_copy(self, m):
		at = m["at"]
		dlg = wx.FileDialog(gui.mainFrame, _("Save as"), defaultFile=at.get("n", "file"), style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
		path = _modal(dlg, lambda d, c: d.GetPath() if c == wx.ID_OK else None)
		if path:
			shutil.copyfile(at["path"], path)
			speak(_("Saved"))

	def copy(self, m):
		if m["kind"] in ("file", "voice") and m.get("at", {}).get("path"):
			text = m["at"]["path"]
		elif m["kind"] == "poll":
			text = self.e._poll_text(m)
		else:
			text = m.get("x", "")
		api.copyToClip(text)
		speak(_("Copied"))

	def forward(self, m):
		chats = []
		targets = [c for c in self.e.chat_list("all") if not (c["type"] == "group" and (c.get("left") or c.get("removed")))]
		if not targets:
			speak(_("There are no chats to forward to."))
			return
		r = pick_many(_("Forward to"), _("&Chats (press Space to select):"), [self.e.chat_title(c) for c in targets])
		if not r:
			return
		n = self.e.forward(m, [targets[i] for i in r])
		speak(_("Forwarded to %d chats") % n)

	def react(self, m):
		labels = ["%s %s" % (em, name) for em, name in EMOJI] + [_("Remove my reaction")]
		i = choose(_("&Reaction:"), _("React"), labels)
		if i < 0:
			return
		mine = m.get("rc", {}).get(self.e.me)
		if i == len(EMOJI):
			if mine:
				self.e.react(self.chat, m, mine)
		else:
			self.e.react(self.chat, m, EMOJI[i][0])
		self.refresh()

	def edit(self, m):
		t = ask_text(_("Edit your message:"), _("Edit message"), m["x"], multiline=True)
		if t is None:
			return
		err = self.e.edit_message(self.chat, m, t)
		if err:
			speak(err)
			wx.MessageBox(err, _("Edit message"), wx.OK | wx.ICON_WARNING, self)
		self.refresh()

	def vote(self, m):
		pl = m["pl"]
		mine = m.get("pl", {}).get("v", {}).get(self.e.me, [])
		if pl.get("m"):
			r = pick_many(_("Vote"), _("&Options (press Space to select):"), pl["o"], mine)
			if r is None:
				return
			idx = r
		else:
			i = choose(_("&Your answer:"), pl["q"], pl["o"])
			if i < 0:
				return
			idx = [i]
		self.e.vote(self.chat, m, idx)
		self.refresh()
		speak(_("Vote sent"))

	def delete_me(self, m):
		if confirm(_("Delete this message for you?"), _("Delete message")):
			self.e.delete_for_me(self.chat, m)
			self.refresh()

	def delete_all(self, m):
		if confirm(_("Delete this message for everyone in the chat?"), _("Delete message")):
			err = self.e.delete_for_everyone(self.chat, m)
			if err:
				speak(err)
			self.refresh()

	def search(self):
		q = ask_text(_("&Search in this chat for:"), _("Search"))
		if not q:
			return
		hits = self.e.search_chat(self.chat, q)
		if not hits:
			speak(_("Nothing found"))
			return
		labels = [self.e.format_message(self.chat, m) for m in hits]
		i = choose(_("%d results:") % len(hits), _("Search results"), labels)
		if i >= 0:
			target = hits[i]
			while target not in self.shown and self.limit < len(self.chat["msgs"]) + 300:
				self.limit += 300
				self.refresh()
			if target in self.shown:
				self.list.SetSelection(self.shown.index(target))
			self.list.SetFocus()

	def onClose(self, ev):
		if self.recorder:
			self.stop_recording(send=False)
		self.e.set_viewing(self.chat, False)
		self.ctrl.window_gone(self.chat["id"])
		if self.ctrl.main:
			self.ctrl.main.refresh()
		self.Destroy()


# ---------------------------------------------------------------- status, starred, results
class StatusDialog(object):
	def __init__(self, ctrl):
		self.ctrl = ctrl
		self.e = ctrl.engine

	def run(self):
		e = self.e
		while True:
			items = []
			for u, lst in e.status_list():
				for s in lst:
					who = _("My status") if u == e.me else e.display_name(u)
					items.append(("%s, %s: %s" % (who, fmt_time(s["ts"]), s["x"]), u, s))
			labels = [i[0] for i in items]
			opts = [_("Post a new status")] + ([_("Delete one of my statuses")] if any(i[1] == e.me for i in items) else []) \
				+ ([_("Read the status updates")] if items else [])
			i = choose(_("Status updates last 24 hours and are seen by your friends. You have %d to read.") % len([x for x in items if x[1] != e.me]), _("Status"), opts)
			if i < 0:
				return
			if opts[i] == _("Post a new status"):
				t = ask_text(_("What is on your mind?"), _("New status"), "", multiline=True)
				if t:
					e.post_status(t)
					speak(_("Status posted"))
			elif opts[i] == _("Delete one of my statuses"):
				mine = [x for x in items if x[1] == e.me]
				j = choose(_("Status to delete:"), _("Delete status"), [x[0] for x in mine])
				if j >= 0:
					e.delete_status(mine[j][2]["id"])
					speak(_("Status deleted"))
			else:
				show_text(_("Status updates"), "\n\n".join(labels))


class ResultList(object):
	"""A list of messages (search results, starred messages). Enter opens the chat."""

	def __init__(self, ctrl, title, rows):
		self.ctrl = ctrl
		self.title = title
		self.rows = rows

	def run(self):
		i = choose(_("%d messages. Choose one to open its chat:") % len(self.rows), self.title, [r[0] for r in self.rows])
		if i >= 0:
			chat = self.rows[i][1]
			self.ctrl.open_chat(chat)
			w = self.ctrl.windows.get(chat["id"])
			if w:
				target = self.rows[i][2]
				while target not in w.shown and w.limit < len(chat["msgs"]) + 300:
					w.limit += 300
					w.refresh()
				if target in w.shown:
					w.list.SetSelection(w.shown.index(target))
					w.list.SetFocus()


def about_text(engine):
	return _(
		"Friends Mirror is a private chat for NVDA users.\n\n"
		"What you can do\n"
		"Chat one to one and in groups. Reply to, forward, edit, delete, star and react to messages. "
		"See sent, delivered and read marks, who is online and typing. Send files, pictures, voice messages, polls and contacts. "
		"Post status updates that last 24 hours. Use broadcast lists, disappearing messages, mute, pin, archive and search. Block people and "
		"accept or refuse message requests. Calls are not included.\n\n"
		"How to start\n"
		"1. Press NVDA+Alt+C, or open the NVDA menu, Tools, Friends Mirror. Sign in with a user name and a password. A new name creates your account.\n"
		"2. Choose New chat, then type your friend's user name. Your friend does the same with yours.\n"
		"3. Type your message and press Enter. In the message list press Enter for message actions such as Reply or Forward.\n\n"
		"Your privacy\n"
		"Messages, files and voice messages are encrypted on your computer. Only the people in the chat can read them. "
		"Compare the safety number in the contact info with your friend to be sure nobody listens in. "
		"Your password creates your keys, so there is no password reset: do not forget it, and use the same name and password on every computer.\n\n"
		"Good to know\n"
		"Messages wait for about 12 hours for friends who are offline, and files for about 3 hours. After that they are lost, so ask your friend to sign in again and send once more. "
		"Your chats are stored on this computer only. Files are limited to 10 MB.")
