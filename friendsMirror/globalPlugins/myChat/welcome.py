# -*- coding: UTF-8 -*-
"""Welcome dialogs (shown once, after NVDA restarts following the install), the support reminder and the desktop icon."""
import json
import os
import wx
import gui
import api
import ui
import globalVars

APP = "Friends Mirror"
EMAILS = "rithishwaran.v0@gmail.com, mahadeesh18@gmail.com"
FOLDER = os.path.join(globalVars.appArgs.configPath, "myChat")
STATE = os.path.join(FOLDER, "welcome.json")
TRIGGER = os.path.join(FOLDER, "open.trigger")
LAUNCHER = os.path.join(FOLDER, "open_friends_mirror.bat")
SHARE = ("Hi! I am using Friends Mirror, a private, end-to-end encrypted chat add-on for NVDA, "
	"built like WhatsApp but without calls. Install it, press NVDA+Alt+C, create your user name and add me.")
_timer = [None]


def _state():
	try:
		with open(STATE, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return {}


def _save(**kw):
	try:
		os.makedirs(FOLDER, exist_ok=True)
		d = _state()
		d.update(kw)
		with open(STATE, "w", encoding="utf-8") as f:
			json.dump(d, f)
	except Exception:
		pass


def ask(title, text, buttons, escape=-1):
	"""A dialog with a read-only message and a row of buttons. Returns the index of the pressed button."""
	dlg = wx.Dialog(gui.mainFrame, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
	s = wx.BoxSizer(wx.VERTICAL)
	tc = wx.TextCtrl(dlg, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP, size=(520, 190))
	s.Add(tc, 1, wx.ALL | wx.EXPAND, 10)
	row = wx.BoxSizer(wx.HORIZONTAL)
	result = [escape]
	for i, label in enumerate(buttons):
		b = wx.Button(dlg, label=label)
		def pressed(e, i=i):
			result[0] = i
			dlg.EndModal(wx.ID_OK)
		b.Bind(wx.EVT_BUTTON, pressed)
		row.Add(b, 0, wx.ALL, 5)
		if i == 0:
			b.SetDefault()
	s.Add(row, 0, wx.ALIGN_CENTER)
	dlg.SetSizer(s)
	s.Fit(dlg)
	dlg.CentreOnScreen()
	tc.SetFocus()
	gui.mainFrame.prePopup()
	try:
		dlg.ShowModal()
	finally:
		dlg.Destroy()
		gui.mainFrame.postPopup()
	return result[0]


def developers():
	"""Dialog 1: who built it, and how to support them. Returns True when the person copied the email IDs."""
	text = (
		"Friends Mirror was built by two friends, Rithishwaran and Mahadeesh, who have been building things like this since a very young age. "
		"Even as kids they planned the features together, wrote the code, tested every dialog with NVDA, and kept improving it "
		"until it felt comfortable to use with a screen reader.\n\n"
		"If you would like to support them, choose Copy email ID and write to them with your feedback, ideas or thanks. "
		"Or choose Remind me later and this message will come back the next time NVDA starts.")
	r = ask("Friends Mirror: the developers", text, ["Copy email ID", "Remind me later"], escape=1)
	if r == 0:
		try:
			api.copyToClip(EMAILS)
			ui.message("Copied: " + EMAILS)
		except Exception:
			pass
		_save(remind=False)
		return True
	_save(remind=True)
	return False


def desktop_dir():
	try:
		import ctypes
		buf = ctypes.create_unicode_buffer(260)
		ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
		if buf.value:
			return buf.value
	except Exception:
		pass
	return os.path.join(os.path.expanduser("~"), "Desktop")


def make_icon():
	"""Put a Friends Mirror icon on the desktop. Opening it asks the running NVDA to open the chat."""
	import sys
	os.makedirs(FOLDER, exist_ok=True)
	with open(LAUNCHER, "w", encoding="utf-8") as f:
		f.write('@echo off\r\ntype nul > "%~dp0open.trigger"\r\n')
	import comtypes.client
	sh = comtypes.client.CreateObject("WScript.Shell")
	lnk = sh.CreateShortcut(os.path.join(desktop_dir(), APP + ".lnk"))
	lnk.TargetPath = LAUNCHER
	lnk.WorkingDirectory = FOLDER
	lnk.WindowStyle = 7
	lnk.Description = "Open " + APP + " (NVDA must be running)"
	lnk.IconLocation = sys.executable
	lnk.Save()


def ask_icon():
	"""Dialog 2."""
	r = ask("Friends Mirror: desktop icon",
		"Do you want to put a Friends Mirror icon on your desktop?\n\nOpening the icon opens Friends Mirror while NVDA is running.",
		["Yes, add the icon", "No, thank you"], escape=1)
	if r != 0:
		return
	try:
		make_icon()
		ask("Friends Mirror: desktop icon", "The Friends Mirror icon was added to your desktop.", ["OK"], escape=0)
	except Exception:
		ask("Friends Mirror: desktop icon", "Sorry, the icon could not be created. You can still open Friends Mirror with NVDA+Alt+C.", ["OK"], escape=0)


def ask_tips():
	"""Dialog 3."""
	r = ask("Friends Mirror: tips",
		"Do you want a short Friends Mirror tip notification every minute?\n\nYou can change this later in Friends Mirror, Profile and privacy.",
		["Yes, show tips", "No, keep it quiet"], escape=1)
	try:
		os.makedirs(FOLDER, exist_ok=True)
		with open(os.path.join(FOLDER, "promo.json"), "w", encoding="utf-8") as f:
			json.dump({"on": r == 0}, f)
	except Exception:
		pass


def ask_share():
	"""Dialog 4."""
	r = ask("Friends Mirror: tell your friends",
		"Friends Mirror is more fun with friends. Do you want to copy a short invitation message that you can paste to a friend?\n\n" + SHARE,
		["Copy invitation", "No, thank you"], escape=1)
	if r == 0:
		try:
			api.copyToClip(SHARE)
			ui.message("Invitation copied")
		except Exception:
			pass


def ask_start():
	"""Dialog 5."""
	ask("Friends Mirror: ready",
		"Friends Mirror is ready.\n\n"
		"Shortcut keys:\n"
		"NVDA+Alt+C opens Friends Mirror.\n"
		"NVDA+Alt+L repeats the last message.\n"
		"NVDA+Alt+U tells you how many messages are unread.\n"
		"You can also open it from the NVDA menu, Tools, Friends Mirror.\n\n"
		"Create your user name and password the first time you open it. Thank you for using Friends Mirror.",
		["OK"], escape=0)


def run_install():
	for step in (developers, ask_icon, ask_tips, ask_share, ask_start):
		try:
			step()
		except Exception:
			pass


def start(plugin_open):
	"""Called from the plugin at every NVDA start: the welcome dialogs the first time, the support reminder later, and the desktop icon trigger."""
	def first():
		if not _state().get("done"):
			_save(done=True)
			run_install()
		elif _state().get("remind"):
			developers()
	wx.CallLater(8000, first)

	def poll():
		if os.path.exists(TRIGGER):
			try:
				os.remove(TRIGGER)
			except Exception:
				pass
			plugin_open()
	t = wx.PyTimer(poll) if hasattr(wx, "PyTimer") else wx.Timer()
	if not hasattr(wx, "PyTimer"):
		t.Notify = poll
	t.Start(1000)
	_timer[0] = t


def stop():
	t = _timer[0]
	_timer[0] = None
	if t:
		try:
			t.Stop()
		except Exception:
			pass
