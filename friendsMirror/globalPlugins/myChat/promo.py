# -*- coding: UTF-8 -*-
"""A short Friends Mirror reminder notification, shown once a minute. The user can turn it off in Settings."""
import os
import json
import wx
import globalVars
from logHandler import log

INTERVAL_MS = 60 * 1000
_timer = [None]
_index = [0]
TIPS = [
	"Chat one to one with your friends, or make groups.",
	"Send voice messages. Press Enter on one to play it.",
	"Send files, polls and contacts to your friends.",
	"Post a 24 hour status update for your friends.",
	"Reply, forward, edit, star and react to messages.",
	"Delete a message for everyone, or use disappearing messages.",
	"See sent, delivered and read marks, online status and typing.",
	"Every message and file is end-to-end encrypted.",
	"Get a notification and a message tone for each new message.",
	"Use broadcast lists, mute, pin, archive, search and block.",
	"Press NVDA+Alt+C to open Friends Mirror. NVDA+Alt+L repeats the last message.",
]


def _path():
	return os.path.join(globalVars.appArgs.configPath, "myChat", "promo.json")


def enabled():
	try:
		with open(_path(), "r", encoding="utf-8") as f:
			return bool(json.load(f).get("on", True))
	except Exception:
		return True


def set_enabled(value):
	try:
		os.makedirs(os.path.dirname(_path()), exist_ok=True)
		with open(_path(), "w", encoding="utf-8") as f:
			json.dump({"on": bool(value)}, f)
	except Exception:
		log.error("Friends Mirror: could not save the reminder setting", exc_info=True)


def _show():
	if not enabled():
		return
	try:
		from . import toast
		toast.show("Friends Mirror", TIPS[_index[0] % len(TIPS)])
		_index[0] += 1
	except Exception:
		log.error("Friends Mirror: reminder failed", exc_info=True)


def start():
	stop()
	t = wx.PyTimer(_show) if hasattr(wx, "PyTimer") else wx.Timer()
	if not hasattr(wx, "PyTimer"):
		t.Notify = _show
	t.Start(INTERVAL_MS)
	_timer[0] = t


def stop():
	t = _timer[0]
	_timer[0] = None
	if t:
		try:
			t.Stop()
		except Exception:
			pass
