# -*- coding: UTF-8 -*-
"""Message transport. The relay server is built in and is never shown to the user."""
import json
import threading
import time
import urllib.request
import uuid

# The relay is a free ntfy server. It only ever sees encrypted text.
SERVER = "https://ntfy.sh"
UA = {"User-Agent": "myChat-NVDA/1.0"}
MAX_BODY = 3900            # ntfy refuses message bodies above 4096 bytes
MAX_FILE = 14 * 1024 * 1024  # the relay accepts files up to 15 MB
TOPICS_PER_STREAM = 40


def _req(url, data=None, headers=None, method=None):
	h = dict(UA)
	h.update(headers or {})
	return urllib.request.Request(url, data=data, headers=h, method=method)


def post(topic, text, cache=True, timeout=20, server=None):
	"""Publish one encrypted envelope (a short text) to a topic."""
	data = text.encode("utf-8")
	if len(data) > MAX_BODY:
		raise ValueError("message too long")
	extra = {"Content-Type": "text/plain; charset=utf-8"}
	if not cache:
		extra["Cache"] = "no"
	with urllib.request.urlopen(_req((server or SERVER) + "/" + topic, data, extra, "POST"), timeout=timeout) as r:
		return json.loads(r.read().decode("utf-8", "replace") or "{}")


def upload(blob, name="file.bin", timeout=120, server=None):
	"""Upload an already encrypted file. Returns its download address."""
	if len(blob) > MAX_FILE:
		raise ValueError("file too large")
	topic = "mcfile" + uuid.uuid4().hex[:24]
	extra = {"Filename": name, "Content-Type": "application/octet-stream"}
	with urllib.request.urlopen(_req((server or SERVER) + "/" + topic, blob, extra, "PUT"), timeout=timeout) as r:
		ev = json.loads(r.read().decode("utf-8", "replace") or "{}")
	url = (ev.get("attachment") or {}).get("url")
	if not url:
		raise RuntimeError("upload failed")
	return url


def download(url, timeout=120, limit=MAX_FILE + 1024):
	with urllib.request.urlopen(_req(url), timeout=timeout) as r:
		data = r.read(limit)
	return data


class Stream(object):
	"""Listens to many topics at once and hands every event to on_event(topic, event_dict).

	set_topics() can be called at any time; the connection is rebuilt. since() returns the
	'since' value for the next connection (a unix time, or a duration such as 12h).
	"""

	def __init__(self, on_event, on_status, since):
		self.on_event = on_event
		self.on_status = on_status
		self.since = since
		self.topics = []
		self._lock = threading.Lock()
		self._workers = {}
		self._stop = threading.Event()

	def set_topics(self, topics):
		topics = sorted(set(topics))
		with self._lock:
			if topics == self.topics:
				return
			self.topics = topics
			for ev, _resp in list(self._workers.values()):
				ev.set()
				self._close(_resp)
			self._workers = {}
			if self._stop.is_set():
				return
			for i in range(0, len(topics), TOPICS_PER_STREAM):
				chunk = topics[i:i + TOPICS_PER_STREAM]
				ev = threading.Event()
				holder = [None]
				self._workers[i] = (ev, holder)
				threading.Thread(target=self._run, args=(chunk, ev, holder), daemon=True).start()

	@staticmethod
	def _close(holder):
		try:
			if holder and holder[0] is not None:
				holder[0].close()
		except Exception:
			pass

	def _run(self, chunk, stop, holder):
		delay = 2
		url_topics = ",".join(chunk)
		while not stop.is_set() and not self._stop.is_set():
			try:
				url = "%s/%s/json?since=%s" % (SERVER, url_topics, self.since())
				with urllib.request.urlopen(_req(url), timeout=100) as resp:
					holder[0] = resp
					self.on_status(True)
					delay = 2
					for raw in resp:
						if stop.is_set():
							break
						raw = raw.strip()
						if not raw:
							continue
						try:
							ev = json.loads(raw.decode("utf-8", "replace"))
						except ValueError:
							continue
						if ev.get("event") == "message":
							self.on_event(ev.get("topic", ""), ev)
			except Exception:
				if stop.is_set() or self._stop.is_set():
					break
				self.on_status(False)
			finally:
				holder[0] = None
			if stop.wait(delay):
				break
			delay = min(delay * 2, 60)

	def stop(self):
		self._stop.set()
		with self._lock:
			for ev, holder in list(self._workers.values()):
				ev.set()
				self._close(holder)
			self._workers = {}
			self.topics = []
