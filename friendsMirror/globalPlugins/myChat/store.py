# -*- coding: UTF-8 -*-
"""Saves the account, contacts and chats in one file in the NVDA configuration folder."""
import json
import os
import threading
import time


def default_data():
	return {
		"v": 1,
		"me": {"priv": "", "name": "", "about": "Hey there! I am using myChat.",
			"read_receipts": True, "speak": True, "sound": True, "typing": True, "background": True,
			"created": int(time.time())},
		"contacts": {},   # chat id -> {name, nick, about, blocked, known, seen}
		"chats": {},      # chat key -> chat dict (see engine)
		"lists": {},      # broadcast lists
		"status": [],     # status updates of friends and mine
		"seen": [],       # ids of messages already handled
		"last_time": 0,
	}


class Store(object):
	def __init__(self, path):
		self.path = path
		self.lock = threading.RLock()
		self.data = default_data()
		self._timer = None
		self.load()

	def load(self):
		try:
			with open(self.path, "r", encoding="utf-8") as f:
				d = json.load(f)
			base = default_data()
			base.update(d)
			base["me"] = dict(default_data()["me"], **d.get("me", {}))
			self.data = base
		except Exception:
			pass

	def save_now(self):
		with self.lock:
			try:
				os.makedirs(os.path.dirname(self.path), exist_ok=True)
				tmp = self.path + ".tmp"
				with open(tmp, "w", encoding="utf-8") as f:
					json.dump(self.data, f, ensure_ascii=False)
				os.replace(tmp, self.path)
			except Exception:
				pass

	def save(self):
		"""Save soon (several changes in a row are saved together)."""
		with self.lock:
			if self._timer is None:
				self._timer = threading.Timer(2.0, self._fire)
				self._timer.daemon = True
				self._timer.start()

	def _fire(self):
		with self.lock:
			self._timer = None
		self.save_now()

	def flush(self):
		with self.lock:
			if self._timer is not None:
				self._timer.cancel()
				self._timer = None
		self.save_now()
