# -*- coding: UTF-8 -*-
"""Network layer of myChat. The message server is fixed here and is never shown in the interface.

Everything that passes through the server is already encrypted by the engine; the server only moves
random-looking bytes between topics.
"""
import json
import queue
import threading
import time
import urllib.error
import urllib.request

SERVER = "https://ntfy.sh"
UA = {"User-Agent": "myChat-NVDA/1.0"}
MAX_FILE = 10 * 1024 * 1024


class NetError(Exception):
	def __init__(self, message, code=0, retry_after=0):
		super().__init__(message)
		self.code = code
		self.retry_after = retry_after


def _open(req, timeout):
	try:
		return urllib.request.urlopen(req, timeout=timeout)
	except urllib.error.HTTPError as e:
		ra = 0
		try:
			ra = int(e.headers.get("Retry-After", "0"))
		except Exception:
			pass
		raise NetError("HTTP %d" % e.code, e.code, ra)
	except Exception as e:
		raise NetError(str(e) or e.__class__.__name__, 0)


def post(topic, text, cache=True, timeout=20):
	headers = dict(UA, **{"Content-Type": "text/plain; charset=utf-8"})
	if not cache:
		headers["Cache"] = "no"
	req = urllib.request.Request("%s/%s" % (SERVER, topic), data=text.encode("utf-8"), headers=headers, method="POST")
	with _open(req, timeout) as r:
		r.read()


def upload(topic, filename, data, timeout=180):
	"""Upload encrypted bytes as an attachment. Returns the download address."""
	headers = dict(UA, **{"Filename": filename, "Content-Type": "application/octet-stream"})
	req = urllib.request.Request("%s/%s" % (SERVER, topic), data=data, headers=headers, method="PUT")
	with _open(req, timeout) as r:
		info = json.loads(r.read().decode("utf-8", "replace"))
	url = (info.get("attachment") or {}).get("url")
	if not url:
		raise NetError("The file was not accepted")
	return url


def download(url, limit=MAX_FILE + 4096, timeout=180):
	if not url.startswith(SERVER + "/"):
		raise NetError("Unknown file address")
	req = urllib.request.Request(url, headers=UA)
	with _open(req, timeout) as r:
		data = r.read(limit + 1)
	if len(data) > limit:
		raise NetError("The file is too large")
	return data


def poll(topic, since="12h", timeout=20):
	"""Read the messages a topic still holds. Returns a list of ntfy events (oldest first)."""
	req = urllib.request.Request("%s/%s/json?poll=1&since=%s" % (SERVER, topic, since), headers=UA)
	out = []
	with _open(req, timeout) as r:
		for raw in r.read().decode("utf-8", "replace").splitlines():
			raw = raw.strip()
			if not raw:
				continue
			try:
				ev = json.loads(raw)
			except ValueError:
				continue
			if ev.get("event") == "message":
				out.append(ev)
	return out


class Sender(threading.Thread):
	"""Sends queued messages one after another, slowly enough for the server and with retries."""

	def __init__(self, on_connection=None):
		super().__init__(daemon=True)
		self.q = queue.Queue()
		self.stop_flag = threading.Event()
		self.on_connection = on_connection

	def put(self, topic, text, cache=True, cb=None):
		"""cb(error_or_None) is called from the sender thread."""
		self.q.put({"topic": topic, "text": text, "cache": cache, "cb": cb, "tries": 0})

	def pending(self):
		return self.q.qsize()

	def stop(self):
		self.stop_flag.set()
		self.q.put(None)

	def run(self):
		while not self.stop_flag.is_set():
			item = self.q.get()
			if item is None:
				break
			try:
				post(item["topic"], item["text"], item["cache"])
			except NetError as e:
				item["tries"] += 1
				limit = 8 if item["cache"] else 2
				if item["tries"] >= limit or e.code in (400, 401, 403, 404, 413):
					if item["cb"]:
						self._cb(item["cb"], str(e))
				else:
					wait = e.retry_after or (5 if e.code == 429 else min(3 * 2 ** item["tries"], 60))
					if self.stop_flag.wait(wait):
						break
					self.q.put(item)
				continue
			if item["cb"]:
				self._cb(item["cb"], None)
			if self.stop_flag.wait(0.3):
				break

	@staticmethod
	def _cb(cb, err):
		try:
			cb(err)
		except Exception:
			pass


class Stream(threading.Thread):
	"""Keeps one streaming connection open to every topic of the user and reports each new message."""

	def __init__(self, topics, get_since, on_event, on_state):
		super().__init__(daemon=True)
		self.topics = list(topics)
		self.get_since = get_since
		self.on_event = on_event
		self.on_state = on_state
		self.stop_flag = threading.Event()
		self.restart_flag = threading.Event()
		self.resp = None

	def set_topics(self, topics):
		topics = list(topics)
		if topics != self.topics:
			self.topics = topics
			self.restart_flag.set()
			self._close()

	def _close(self):
		try:
			if self.resp is not None:
				self.resp.close()
		except Exception:
			pass

	def stop(self):
		self.stop_flag.set()
		self._close()

	def run(self):
		delay = 2
		while not self.stop_flag.is_set():
			self.restart_flag.clear()
			url = "%s/%s/json?since=%s" % (SERVER, ",".join(self.topics), self.get_since())
			try:
				req = urllib.request.Request(url, headers=UA)
				with _open(req, 100) as resp:
					self.resp = resp
					delay = 2
					self.on_state(True)
					for raw in resp:
						if self.stop_flag.is_set() or self.restart_flag.is_set():
							break
						raw = raw.strip()
						if not raw:
							continue
						try:
							ev = json.loads(raw.decode("utf-8", "replace"))
						except ValueError:
							continue
						if ev.get("event") == "message":
							self.on_event(ev)
			except Exception:
				pass
			finally:
				self.resp = None
			if self.stop_flag.is_set():
				break
			if self.restart_flag.is_set():
				continue
			self.on_state(False)
			if self.stop_flag.wait(delay):
				break
			delay = min(delay * 2, 60)
