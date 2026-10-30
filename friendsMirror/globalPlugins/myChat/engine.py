# -*- coding: UTF-8 -*-
"""The chat engine of myChat: accounts, contacts, one-to-one chats, groups, receipts, files, status and more.

The engine has no user interface. The interface gives it two things:
  call(fn, *args)       runs fn in the NVDA main thread (all engine state is only touched there)
  emit(kind, **data)    receives news from the engine (new message, receipt, typing and so on)
Everything sent to the server is encrypted end to end (see crypto.py).
"""
import json
import mimetypes
import os
import re
import threading
import time

try:
	import addonHandler
	addonHandler.initTranslation()
except Exception:
	def _(s):
		return s

from . import crypto
from . import transport
from .transport import NetError

USER_RE = re.compile(r"^[a-z0-9_.]{3,24}$")
MAX_TEXT = 1200            # characters in one message; longer text is split into several messages
EDIT_WINDOW = 15 * 60      # seconds in which a message can be edited
DELETE_WINDOW = 48 * 3600  # seconds in which a message can be deleted for everyone
STATUS_LIFE = 24 * 3600
HISTORY_LIMIT = 1500       # messages kept per chat
PING_EVERY = 40
ONLINE_WINDOW = 100
LIVE_AGE = 30              # messages younger than this many seconds are "live" (spoken); older ones are backlog

EMOJI = [("\U0001F44D", _("Thumbs up")), ("❤️", _("Heart")), ("\U0001F602", _("Laughing")),
	("\U0001F62E", _("Surprised")), ("\U0001F622", _("Sad")), ("\U0001F64F", _("Thanks")),
	("\U0001F525", _("Fire")), ("\U0001F389", _("Celebrate")), ("\U0001F44F", _("Clap")),
	("\U0001F60D", _("Love it")), ("\U0001F914", _("Thinking")), ("\U0001F44E", _("Thumbs down"))]

DISAPPEAR = [(0, _("Off")), (24 * 3600, _("24 hours")), (7 * 24 * 3600, _("7 days")), (90 * 24 * 3600, _("90 days"))]
MUTE = [(8 * 3600, _("8 hours")), (7 * 24 * 3600, _("1 week")), (-1, _("Always"))]

DEFAULT_SETTINGS = {"speak": "full", "sound": True, "autodl": True, "show_online": True,
	"read_receipts": True, "typing": True, "announce_typing": False, "remember": True,
	"popup": True, "announce_new": True, "popup_preview": True, "tone": True, "announce_online": True, "tone_muted": False, "speak_always": True}


def now():
	return int(time.time())


def new_id():
	return os.urandom(8).hex()


def norm_user(name):
	return re.sub(r"\s+", "", str(name or "")).lower()


def fmt_time(ts):
	t = time.localtime(ts)
	today = time.localtime()
	if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
		return time.strftime("%H:%M", t)
	return time.strftime("%d %b %H:%M", t)


def fmt_size(n):
	if n < 1024:
		return "%d B" % n
	if n < 1024 * 1024:
		return "%.0f KB" % (n / 1024.0)
	return "%.1f MB" % (n / 1024.0 / 1024.0)


def snippet(text, n=60):
	text = " ".join(str(text or "").split())
	return text if len(text) <= n else text[:n - 1] + "…"


class Engine(object):
	def __init__(self, data_dir, call, emit):
		self.dir = data_dir
		self.call = call
		self._emit_fn = emit
		self.gen = 0
		self.reset_state()
		try:
			os.makedirs(self.dir, exist_ok=True)
		except Exception:
			pass

	def reset_state(self):
		self.me = None
		self.priv = None
		self.pub = None
		self.pk = None
		self.d = None
		self.sender = None
		self.stream = None
		self.connected = False
		self.topic_map = {}
		self.topics = []
		self.typing = {}
		self.typing_sent = {}
		self.ack_q = {}
		self.viewing = set()
		self.seen_set = set()
		self.dirty = False
		self.conn_time = 0
		self.catchup = {}
		self.catchup_done = True
		self.last_ping = 0
		self.last_dir = 0
		self.last_clean = 0
		self.last_save = 0
		self.stop_ticker = threading.Event()

	def emit(self, kind, **data):
		try:
			self._emit_fn(kind, **data)
		except Exception:
			pass

	# ------------------------------------------------------------ files and storage
	def _path(self, username):
		return os.path.join(self.dir, "account_%s.json" % username)

	def _session_path(self):
		return os.path.join(self.dir, "session.json")

	def files_dir(self):
		p = os.path.join(self.dir, "files", self.me or "nobody")
		os.makedirs(p, exist_ok=True)
		return p

	def _new_store(self, username, pkb):
		return {"v": 1, "acct": {"u": username, "pk": pkb, "name": username,
			"about": _("Hey there! I am using myChat."), "ps": crypto.b64(os.urandom(24))},
			"settings": dict(DEFAULT_SETTINGS), "contacts": {}, "chats": {}, "status": {}, "seen": [],
			"cursor": 0, "dir_ok": 0}

	def save(self, force=True):
		if not self.d:
			return
		self.d["seen"] = self.d["seen"][-3000:]
		tmp = self._path(self.me) + ".tmp"
		try:
			with open(tmp, "w", encoding="utf-8") as f:
				json.dump(self.d, f, ensure_ascii=False)
			os.replace(tmp, self._path(self.me))
			self.dirty = False
		except Exception:
			pass

	def touch(self):
		self.dirty = True

	@property
	def settings(self):
		return self.d["settings"]

	def setting(self, key):
		return self.d["settings"].get(key, DEFAULT_SETTINGS.get(key))

	def set_setting(self, key, value):
		self.d["settings"][key] = value
		self.touch()
		if key == "show_online":
			self._share_profile_all()
			self._beat(True)

	# ------------------------------------------------------------ sign in
	def saved_session(self):
		try:
			with open(self._session_path(), "r", encoding="utf-8") as f:
				s = json.load(f)
			return s.get("u"), crypto.unb64(s.get("k"))
		except Exception:
			return None, None

	def last_username(self):
		try:
			with open(self._session_path(), "r", encoding="utf-8") as f:
				return json.load(f).get("u", "")
		except Exception:
			return ""

	def auto_login(self):
		"""Sign in again with the saved key (if the user chose to stay signed in). Returns True when a session started."""
		u, priv = self.saved_session()
		if not u or not priv or not os.path.isfile(self._path(u)):
			return False
		try:
			with open(self._path(u), "r", encoding="utf-8") as f:
				store = json.load(f)
			pub = crypto.public_key(priv)
			if store["acct"]["pk"] != crypto.b64(pub):
				return False
		except Exception:
			return False
		self._start_session(u, priv, pub, store, True)
		return True

	def login(self, username, password, remember, cb):
		"""Create the account or sign in. cb(ok, message) runs in the main thread.

		The same user name and password always give the same keys, so there is no separate sign-up."""
		u = norm_user(username)
		if not USER_RE.match(u):
			cb(False, _("The user name must have 3 to 24 letters, digits, dots or underscores, without spaces."))
			return
		if len(password) < 6:
			cb(False, _("The password must have at least 6 characters."))
			return
		if self.me:
			self.logout()

		def work():
			priv, pub = crypto.account_keys(u, password)
			pkb = crypto.b64(pub)
			store = None
			try:
				with open(self._path(u), "r", encoding="utf-8") as f:
					store = json.load(f)
			except Exception:
				store = None
			if store and store["acct"]["pk"] != pkb:
				return (False, _("Wrong password for this user name."))
			online = True
			try:
				recs = self._directory_records(u)
			except NetError:
				recs = None
				online = False
			if recs:
				first = recs[0][1]
				if first != pkb:
					return (False, _("This user name is already taken, or the password is wrong. If it is your name, type the password you used when you created it."))
			if recs is None and not store:
				return (False, _("There is no internet connection. Connect to the internet to create your account."))
			if store is None:
				store = self._new_store(u, pkb)
			return (True, (priv, pub, store))

		def done(r):
			if isinstance(r, Exception):
				cb(False, _("Error: %s") % r)
				return
			ok, payload = r
			if not ok:
				cb(False, payload)
				return
			priv, pub, store = payload
			self._start_session(u, priv, pub, store, remember)
			cb(True, _("Signed in as %s") % u)
		self._bg(work, done)

	def _bg(self, fn, cb):
		def w():
			try:
				r = fn()
			except Exception as e:
				r = e
			self.call(cb, r)
		threading.Thread(target=w, daemon=True).start()

	def _directory_records(self, username):
		"""All (server time, public key) claims for a user name, oldest first."""
		out = []
		for ev in transport.poll(crypto.directory_topic(username), "12h"):
			try:
				rec = json.loads(ev.get("message", ""))
			except ValueError:
				continue
			if isinstance(rec, dict) and norm_user(rec.get("u")) == username and rec.get("k"):
				out.append((ev.get("time", 0), rec["k"]))
		out.sort()
		return out

	def _start_session(self, username, priv, pub, store, remember):
		self.reset_state()
		self.me, self.priv, self.pub = username, priv, pub
		self.pk = crypto.b64(pub)
		self.d = store
		for k, v in DEFAULT_SETTINGS.items():
			self.d["settings"].setdefault(k, v)
		self.d["settings"]["remember"] = bool(remember)
		self.seen_set = set(self.d.get("seen", []))
		self.gen += 1
		try:
			if remember:
				with open(self._session_path(), "w", encoding="utf-8") as f:
					json.dump({"u": username, "k": crypto.b64(priv)}, f)
			else:
				with open(self._session_path(), "w", encoding="utf-8") as f:
					json.dump({"u": username}, f)
		except Exception:
			pass
		self.save()
		self.sender = transport.Sender()
		self.sender.start()
		self._rebuild_topics(start=True)
		self.conn_time = now()
		self.catchup_done = False
		self.stop_ticker = threading.Event()
		gen = self.gen
		stop = self.stop_ticker

		def ticker():
			while not stop.wait(1):
				self.call(self._tick, gen)
		threading.Thread(target=ticker, daemon=True).start()
		self._publish_directory()
		self._beat(True)
		self.emit("login", user=username)

	def shutdown(self):
		"""NVDA is closing: stop everything but stay signed in for next time."""
		self.logout(keep_session=True)

	def logout(self, forget=False, keep_session=False):
		if not self.me:
			return
		try:
			self._beat(False)
		except Exception:
			pass
		self.save()
		if self.stream:
			self.stream.stop()
		if self.sender:
			time.sleep(0.2)
			self.sender.stop()
		self.stop_ticker.set()
		me = self.me
		self.gen += 1
		self.reset_state()
		try:
			if not keep_session:
				with open(self._session_path(), "w", encoding="utf-8") as f:
					json.dump({"u": me}, f)
			if forget:
				os.remove(self._path(me))
		except Exception:
			pass
		self.emit("logout")

	# ------------------------------------------------------------ topics and the live connection
	def _rebuild_topics(self, start=False):
		topics = [crypto.inbox_topic(self.me, self.pk)]
		tmap = {topics[0]: ("inbox", None)}
		for chat in self.d["chats"].values():
			if chat["type"] == "group" and not chat.get("left") and not chat.get("removed"):
				t = crypto.group_topic(chat["gid"])
				topics.append(t)
				tmap[t] = ("group", chat["gid"])
		for u, c in self.d["contacts"].items():
			if c.get("accepted") and not c.get("blocked") and c.get("ps"):
				t = crypto.presence_topic(c["ps"])
				topics.append(t)
				tmap[t] = ("pres", u)
		self.topics, self.topic_map = topics, tmap
		if start:
			self.stream = transport.Stream(topics, self._since,
				lambda ev: self.call(self._on_net_event, ev, self.gen),
				lambda ok: self.call(self._on_conn, ok, self.gen))
			self.stream.start()
		elif self.stream:
			self.stream.set_topics(topics)

	def _since(self):
		cur = self.d["cursor"] if self.d else 0
		floor = now() - 12 * 3600
		return max(int(cur) - 10, floor)

	def _on_conn(self, ok, gen):
		if gen != self.gen:
			return
		if ok != self.connected:
			self.connected = ok
			if ok:
				self.conn_time = now()
			self.emit("connection", connected=ok)

	# ------------------------------------------------------------ incoming
	def _on_net_event(self, ev, gen):
		if gen != self.gen or not self.me:
			return
		eid = ev.get("id")
		if eid:
			if eid in self.seen_set:
				return
			self.seen_set.add(eid)
			self.d["seen"].append(eid)
		t = int(ev.get("time") or now())
		if t > self.d["cursor"]:
			self.d["cursor"] = t
		self.touch()
		route = self.topic_map.get(ev.get("topic"))
		if not route:
			return
		try:
			obj = json.loads(ev.get("message", ""))
		except ValueError:
			return
		if not isinstance(obj, dict):
			return
		live = (now() - t) < LIVE_AGE
		kind, arg = route
		try:
			if kind == "inbox":
				self._on_dm(obj, t, live)
			elif kind == "group":
				self._on_group(arg, obj, t, live)
			elif kind == "pres":
				self._on_presence(arg, obj, t)
		except Exception as e:
			self.emit("error", text="%s" % e)

	def _on_dm(self, obj, t, live):
		f = norm_user(obj.get("f"))
		kpk = obj.get("k")
		blob = obj.get("c")
		if not (f and kpk and blob) or f == self.me or not USER_RE.match(f):
			return
		con = self.d["contacts"].get(f)
		if con and con.get("blocked"):
			return
		if con and con.get("stub"):
			con["pk"], con["stub"] = kpk, False
		if con and con["pk"] != kpk:
			if con.get("new_pk") != kpk:
				con["new_pk"] = kpk
				chat = self.dm_chat(f)
				self._sys(chat, _("Warning: the security key of %s changed. Messages from the new key are blocked until you trust it in the contact info.") % self.display_name(f))
				self.emit("warning", chat=chat, user=f)
			return
		try:
			key = crypto.pair_key(self.priv, self.pub, crypto.unb64(kpk))
			ev = json.loads(crypto.open_sealed(key, crypto.unb64(blob), ("dm|%s|%s" % (f, self.me)).encode()).decode("utf-8"))
		except Exception:
			return
		if not isinstance(ev, dict):
			return
		if con is None:
			con = self._new_contact(f, kpk, accepted=False)
			self._check_directory(f, kpk)
		self._dispatch(f, None, ev, t, live)

	def _on_group(self, gid, obj, t, live):
		chat = self.d["chats"].get("g:" + gid)
		if not chat or chat.get("left") or chat.get("removed"):
			return
		ver = str(obj.get("r", chat["ver"]))
		keyb = chat["keys"].get(ver)
		if not keyb:
			return
		try:
			ev = json.loads(crypto.open_sealed(crypto.unb64(keyb), crypto.unb64(obj.get("c", "")),
				("grp|%s|%s" % (gid, ver)).encode()).decode("utf-8"))
		except Exception:
			return
		if not isinstance(ev, dict):
			return
		f = norm_user(ev.get("f"))
		if f == self.me or f not in chat["members"]:
			return
		con = self.d["contacts"].get(f)
		if con and con.get("blocked"):
			return
		self._dispatch(f, chat, ev, t, live)

	def _on_presence(self, user, obj, t):
		con = self.d["contacts"].get(user)
		if not con or not con.get("ps") or con.get("blocked"):
			return
		try:
			key = crypto.hkdf(crypto.unb64(con["ps"]), b"", b"myChat presence", 64)
			rec = json.loads(crypto.open_sealed(key, crypto.unb64(obj.get("c", "")), b"pres").decode("utf-8"))
		except Exception:
			return
		was = self.is_online(user)
		con["on"] = bool(rec.get("on"))
		if con["on"] and not was:
			con["on_since"] = t
		con["pr_ts"] = time.time() if (now() - t) < 60 else t
		con["seen"] = max(con.get("seen", 0), t)
		self.touch()
		if was != self.is_online(user):
			self.emit("presence", user=user, online=self.is_online(user))

	def _dispatch(self, sender, chat, ev, t, live):
		typ = ev.get("t")
		con = self.d["contacts"].get(sender)
		if con is None:
			return
		if chat is None:
			chat = self.dm_chat(sender)
		if chat["type"] == "dm" and not con.get("accepted") and typ not in ("m", "pf", "gi", "a"):
			return
		con["seen"] = max(con.get("seen", 0), t)
		handler = getattr(self, "_h_" + str(typ), None)
		if handler:
			handler(chat, sender, ev, t, live)

	def _find(self, chat, mid):
		for m in reversed(chat["msgs"]):
			if m["id"] == mid:
				return m
		return None

	def _h_m(self, chat, sender, ev, t, live):
		mid = str(ev.get("id") or "")
		if not mid or self._find(chat, mid):
			return
		con = self.d["contacts"][sender]
		msg = {"id": mid, "ts": t, "f": sender, "x": str(ev.get("x") or ""), "kind": ev.get("kind") or "text",
			"own": False, "unr": True}
		for k in ("rp", "fw", "at", "pl", "ct"):
			if ev.get(k):
				msg[k] = ev[k]
		if msg["kind"] == "poll" and isinstance(msg.get("pl"), dict):
			msg["pl"]["v"] = {}
		ex = ev.get("ex")
		if ex:
			try:
				msg["ex"] = t + int(ex)
			except (TypeError, ValueError):
				pass
		if chat["type"] == "dm" and not con.get("accepted"):
			chat["request"] = True
		self._insert(chat, msg)
		if not (chat["type"] == "dm" and chat.get("request")):
			self._queue_ack(sender, chat, mid, 2)
		chat["archived"] = chat.get("archived") and self.is_muted(chat)
		if chat["id"] in self.viewing:
			self.mark_read(chat)
		else:
			chat["unread"] = chat.get("unread", 0) + 1
		at = msg.get("at")
		if at and at.get("u") and self.setting("autodl") and at.get("s", 0) <= transport.MAX_FILE and not chat.get("request"):
			self.download_attachment(chat, msg)
		self.emit("message", chat=chat, msg=msg, live=live, backlog=not live)

	def _h_a(self, chat, sender, ev, t, live):
		gid = ev.get("g")
		target = self.d["chats"].get("g:" + gid) if gid else self.dm_chat(sender)
		if not target:
			return
		s = ev.get("s")
		for mid in ev.get("ids", [])[:200]:
			m = self._find(target, str(mid))
			if m and m.get("own"):
				m.setdefault("dl", {})[sender] = t
				if s == 3:
					m.setdefault("rd", {})[sender] = t
		self.touch()
		self.emit("receipt", chat=target)

	def _h_ty(self, chat, sender, ev, t, live):
		if (now() - t) > 15:
			return
		if ev.get("on"):
			self.typing.setdefault(chat["id"], {})[sender] = time.time() + 6
		else:
			self.typing.get(chat["id"], {}).pop(sender, None)
		self.emit("typing", chat=chat, text=self.typing_text(chat))

	def _h_ed(self, chat, sender, ev, t, live):
		m = self._find(chat, str(ev.get("id")))
		if m and m.get("f") == sender and not m.get("own") and not m.get("deleted"):
			m["x"] = str(ev.get("x") or "")
			m["edited"] = True
			self.touch()
			self.emit("msgupdate", chat=chat, msg=m, live=live, note=_("%s edited a message: %s") % (self.display_name(sender), snippet(m["x"])))

	def _h_dl(self, chat, sender, ev, t, live):
		m = self._find(chat, str(ev.get("id")))
		if m and m.get("f") == sender and not m.get("deleted"):
			self._wipe(m)
			self.touch()
			self.emit("msgupdate", chat=chat, msg=m, live=live, note=_("%s deleted a message") % self.display_name(sender))

	def _wipe(self, m):
		m["deleted"] = True
		m["x"] = ""
		for k in ("at", "pl", "rp", "ct", "rc", "fw"):
			m.pop(k, None)

	def _h_rc(self, chat, sender, ev, t, live):
		m = self._find(chat, str(ev.get("id")))
		if not m or m.get("deleted"):
			return
		e = str(ev.get("e") or "")
		rc = m.setdefault("rc", {})
		if e:
			rc[sender] = e[:8]
		else:
			rc.pop(sender, None)
		self.touch()
		note = _("%s reacted %s to: %s") % (self.display_name(sender), e, snippet(m.get("x") or _("a message"), 40)) if e and m.get("own") else None
		self.emit("msgupdate", chat=chat, msg=m, live=live, note=note)

	def _h_pv(self, chat, sender, ev, t, live):
		m = self._find(chat, str(ev.get("id")))
		if not m or not m.get("pl"):
			return
		n = len(m["pl"].get("o", []))
		idx = sorted(set(i for i in ev.get("o", []) if isinstance(i, int) and 0 <= i < n))
		if not m["pl"].get("m"):
			idx = idx[:1]
		m["pl"].setdefault("v", {})[sender] = idx
		self.touch()
		self.emit("msgupdate", chat=chat, msg=m, live=live, note=None)

	def _h_pf(self, chat, sender, ev, t, live):
		con = self.d["contacts"][sender]
		if ev.get("n"):
			con["name"] = snippet(ev["n"], 40)
		con["about"] = snippet(ev.get("a", ""), 140)
		ps = ev.get("ps") or ""
		if ps != con.get("ps", ""):
			con["ps"] = ps
			if not ps:
				con["on"] = False
			self._rebuild_topics()
		self.touch()
		self.emit("chats")

	def _h_ds(self, chat, sender, ev, t, live):
		if chat["type"] == "group" and sender not in chat["admins"]:
			return
		sec = int(ev.get("s") or 0)
		if sec != chat.get("disap", 0):
			chat["disap"] = sec
			self._sys(chat, self._disap_note(sender, sec), live=live)

	def _disap_note(self, who, sec):
		name = _("You") if who == self.me else self.display_name(who)
		if not sec:
			return _("%s turned off disappearing messages") % name
		label = dict(DISAPPEAR).get(sec, "%d s" % sec)
		return _("%s set messages to disappear after %s") % (name, label)

	def _h_st(self, chat, sender, ev, t, live):
		sid = str(ev.get("id") or "")
		lst = self.d["status"].setdefault(sender, [])
		if sid and not any(s["id"] == sid for s in lst):
			lst.append({"id": sid, "x": str(ev.get("x") or ""), "ts": t})
			self.touch()
			self.emit("status", user=sender, live=live)

	def _h_sd(self, chat, sender, ev, t, live):
		lst = self.d["status"].get(sender, [])
		self.d["status"][sender] = [s for s in lst if s["id"] != str(ev.get("id"))]
		self.touch()

	def _h_gi(self, chat, sender, ev, t, live):
		"""A group invitation or group change, always sent to us as a private encrypted message."""
		gid = str(ev.get("gid") or "")
		if not gid or not re.match(r"^[a-f0-9]{8,32}$", gid):
			return
		cid = "g:" + gid
		g = self.d["chats"].get(cid)
		members = [norm_user(x) for x in ev.get("mem", []) if USER_RE.match(norm_user(x))][:256]
		admins = [norm_user(x) for x in ev.get("adm", []) if norm_user(x) in members]
		ver = int(ev.get("ver") or 1)
		if g is not None:
			if sender not in g["admins"] or ver < g["ver"]:
				return
		if self.me not in members:
			if g is not None and not g.get("removed"):
				g["removed"] = True
				self._sys(g, _("You were removed from this group"), live=live)
				self._rebuild_topics()
			return
		key = ev.get("key")
		if not key:
			return
		new = g is None
		if new:
			g = self._blank_chat(cid, "group")
			g.update({"gid": gid, "name": snippet(ev.get("n") or _("Group"), 40), "about": "", "members": [], "admins": [],
				"ver": 0, "keys": {}, "creator": sender, "left": False, "removed": False})
			self.d["chats"][cid] = g
		old_members = set(g["members"])
		g["name"] = snippet(ev.get("n") or g["name"], 40)
		g["about"] = snippet(ev.get("ab") or "", 200)
		g["members"], g["admins"] = members, admins or members[:1]
		g["keys"][str(ver)] = key
		g["ver"] = max(g["ver"], ver)
		g["removed"] = False
		g["left"] = False
		for u in members:
			if u != self.me and u not in self.d["contacts"]:
				self._new_contact(u, ev.get("pks", {}).get(u, ""), accepted=False, stub=True)
		if new:
			self._sys(g, _("%s added you to the group %s") % (self.display_name(sender), g["name"]), live=live)
		else:
			for u in members:
				if u not in old_members:
					self._sys(g, _("%s added %s") % (self.display_name(sender), self.display_name(u)), live=live)
			for u in old_members:
				if u not in members:
					self._sys(g, _("%s removed %s") % (self.display_name(sender), self.display_name(u)), live=live)
		self._rebuild_topics()
		self.touch()
		self.emit("chats")
		if new:
			self.emit("message", chat=g, msg=g["msgs"][-1], live=live, backlog=not live)

	def _h_gl(self, chat, sender, ev, t, live):
		if chat["type"] != "group":
			return
		if sender in chat["members"]:
			chat["members"].remove(sender)
		if sender in chat["admins"]:
			chat["admins"].remove(sender)
		if not chat["admins"] and chat["members"]:
			chat["admins"] = [chat["members"][0]]
		self._sys(chat, _("%s left the group") % self.display_name(sender), live=live)
		# the first remaining admin changes the key, so the person who left can no longer read new messages
		if chat["admins"] and chat["admins"][0] == self.me and not chat.get("left"):
			self._group_push(chat)

	# ------------------------------------------------------------ contacts and chats
	def _new_contact(self, u, pkb, accepted, stub=False):
		c = {"u": u, "name": u, "pk": pkb, "about": "", "ps": "", "accepted": accepted, "blocked": False,
			"seen": 0, "on": False, "pr_ts": 0, "verified": False, "stub": stub}
		self.d["contacts"][u] = c
		self.touch()
		return c

	def display_name(self, u):
		if u == self.me:
			return _("You")
		c = self.d["contacts"].get(u)
		if not c:
			return u
		return c.get("nick") or (c.get("name") if c.get("name") and c.get("name") != u else u)

	def _blank_chat(self, cid, typ):
		return {"id": cid, "type": typ, "msgs": [], "unread": 0, "pinned": False, "archived": False,
			"muted": 0, "disap": 0, "draft": "", "last_ts": now(), "request": False}

	def dm_chat(self, u, create=True):
		cid = "d:" + u
		c = self.d["chats"].get(cid)
		if c is None and create:
			c = self._blank_chat(cid, "dm")
			c["peer"] = u
			c["disap"] = self.setting("default_disap") or 0
			self.d["chats"][cid] = c
			self.touch()
		return c

	def chat_title(self, chat):
		if chat["type"] == "dm":
			return self.display_name(chat["peer"])
		return chat.get("name", "")

	def is_muted(self, chat):
		m = chat.get("muted", 0)
		return m == -1 or (m and m > now())

	def _insert(self, chat, msg):
		msgs = chat["msgs"]
		i = len(msgs)
		while i > 0 and msgs[i - 1]["ts"] > msg["ts"]:
			i -= 1
		msgs.insert(i, msg)
		if len(msgs) > HISTORY_LIMIT:
			del msgs[0]
		chat["last_ts"] = max(chat.get("last_ts", 0), msg["ts"])
		self.touch()

	def _sys(self, chat, text, live=False):
		msg = {"id": new_id(), "ts": now(), "f": "", "x": text, "kind": "sys", "own": False}
		if chat.get("disap"):
			msg["ex"] = msg["ts"] + chat["disap"]
		self._insert(chat, msg)
		self.emit("sysmessage", chat=chat, msg=msg, live=live)

	def chat_list(self, mode="all"):
		out = []
		for chat in self.d["chats"].values():
			if chat["type"] == "dm" and chat.get("request"):
				if mode == "requests":
					out.append(chat)
				continue
			if mode == "requests":
				continue
			if mode == "archived":
				if chat.get("archived"):
					out.append(chat)
				continue
			if chat.get("archived"):
				continue
			if mode == "unread" and not chat.get("unread"):
				continue
			if mode == "groups" and chat["type"] != "group":
				continue
			if mode == "all" and chat["type"] == "dm" and not chat["msgs"] and not chat.get("keep"):
				continue
			out.append(chat)
		out.sort(key=lambda c: (not c.get("pinned"), -c.get("last_ts", 0)))
		return out

	def unread_total(self):
		return sum(c.get("unread", 0) for c in self.d["chats"].values() if not c.get("request"))

	def preview(self, chat):
		for m in reversed(chat["msgs"]):
			if m["kind"] == "sys":
				continue
			return self.msg_summary(chat, m)
		return ""

	def msg_summary(self, chat, m):
		who = "" if chat["type"] == "dm" and not m.get("own") else (_("You") if m.get("own") else self.display_name(m.get("f", ""))) + ": "
		if chat["type"] == "dm" and m.get("own"):
			who = _("You") + ": "
		if m.get("deleted"):
			return who + _("This message was deleted")
		k = m["kind"]
		if k == "voice":
			return who + _("Voice message")
		if k == "file":
			return who + _("File: %s") % m.get("at", {}).get("n", "")
		if k == "poll":
			return who + _("Poll: %s") % m.get("pl", {}).get("q", "")
		if k == "contact":
			return who + _("Contact: %s") % m.get("ct", {}).get("u", "")
		return who + snippet(m.get("x", ""), 80)

	# ------------------------------------------------------------ resolving and adding people
	def resolve(self, username, cb):
		"""Find the public key of a user. cb(pk_b64 or None, error_text) runs in the main thread."""
		u = norm_user(username)
		if not USER_RE.match(u):
			cb(None, _("That is not a valid user name."))
			return

		def work():
			recs = self._directory_records(u)
			return recs[0][1] if recs else None

		def done(r):
			if isinstance(r, Exception):
				cb(None, _("Error: %s") % r)
			elif r is None:
				cb(None, _("No user called %s was found. Ask your friend to sign in to myChat once, then try again.") % u)
			else:
				cb(r, "")
		self._bg(work, done)

	def add_contact(self, username, cb):
		"""Add a friend by user name. cb(ok, text_or_username)"""
		u = norm_user(username)
		if u == self.me:
			# Chat with yourself, to test myChat. Nothing is sent to the network.
			if u not in self.d["contacts"]:
				self._new_contact(u, self.pk, accepted=True)
			self.d["contacts"][u].update(accepted=True, blocked=False, stub=False)
			chat = self.dm_chat(u)
			chat["keep"] = True
			chat["request"] = False
			self.touch()
			self.emit("chats")
			cb(True, u)
			return
		con = self.d["contacts"].get(u)
		if con and con.get("accepted") and not con.get("stub"):
			cb(True, u)
			return
		if con and con.get("pk") and not con.get("stub"):
			self.accept_request(self.dm_chat(u))
			cb(True, u)
			return

		def got(pk, err):
			if not pk:
				cb(False, err)
				return
			c = self.d["contacts"].get(u)
			if c is None:
				c = self._new_contact(u, pk, accepted=True)
			else:
				c["pk"], c["stub"], c["accepted"] = pk, False, True
			chat = self.dm_chat(u)
			chat["keep"] = True
			chat["request"] = False
			self._send_profile(u)
			self._rebuild_topics()
			self.touch()
			self.emit("chats")
			cb(True, u)
		self.resolve(u, got)

	def _check_directory(self, u, pkb):
		"""For a stranger who writes to us: compare their key with the public directory (warns about impersonation)."""
		def work():
			recs = self._directory_records(u)
			return (recs[0][1] if recs else "")

		def done(r):
			if isinstance(r, Exception) or not self.me:
				return
			c = self.d["contacts"].get(u)
			if c and r and r != pkb and not c.get("accepted"):
				c["suspicious"] = True
				self._sys(self.dm_chat(u), _("Warning: this person's key does not match the public entry of the user name %s. It may be someone pretending to be that user.") % u)
				self.touch()
		self._bg(work, done)

	def accept_request(self, chat):
		u = chat["peer"]
		c = self.d["contacts"].get(u)
		if not c:
			return
		c["accepted"] = True
		c["blocked"] = False
		chat["request"] = False
		chat["keep"] = True
		self._send_profile(u)
		self._rebuild_topics()
		unread = [m["id"] for m in chat["msgs"] if not m.get("own") and m.get("unr") is not None and m["kind"] != "sys"]
		for mid in unread:
			self._queue_ack(u, chat, mid, 2)
		self.touch()
		self.emit("chats")

	def block(self, u, on=True):
		c = self.d["contacts"].get(u)
		if not c:
			return
		c["blocked"] = bool(on)
		if on:
			c["on"] = False
			self.d["status"].pop(u, None)
			self.d["acct"]["ps"] = crypto.b64(os.urandom(24))
			self._share_profile_all()
		self._rebuild_topics()
		self.touch()
		self.emit("chats")

	def blocked_list(self):
		return [u for u, c in self.d["contacts"].items() if c.get("blocked")]

	def contacts_list(self, accepted_only=True):
		out = [c for c in self.d["contacts"].values() if (c.get("accepted") or not accepted_only) and not c.get("stub")]
		out.sort(key=lambda c: self.display_name(c["u"]).lower())
		return out

	def rename_contact(self, u, nick):
		c = self.d["contacts"].get(u)
		if c:
			c["nick"] = snippet(nick, 40) if nick.strip() else ""
			self.touch()

	def trust_new_key(self, u):
		c = self.d["contacts"].get(u)
		if c and c.get("new_pk"):
			c["pk"], c["new_pk"], c["verified"] = c["new_pk"], None, False
			self._rebuild_topics()
			self._send_profile(u)
			self._sys(self.dm_chat(u), _("You accepted the new security key of %s.") % self.display_name(u))
			self.touch()

	def safety_number(self, u):
		c = self.d["contacts"].get(u)
		if not c or not c.get("pk"):
			return ""
		return crypto.safety_number(self.pub, crypto.unb64(c["pk"]))

	# ------------------------------------------------------------ sending plumbing
	def _send_dm_event(self, peer, event, cache=True, cb=None):
		if peer == self.me:
			if cb:
				cb(None)
			return
		c = self.d["contacts"].get(peer)
		if not c or not c.get("pk"):
			if cb:
				cb(_("Unknown contact"))
			return
		try:
			key = crypto.pair_key(self.priv, self.pub, crypto.unb64(c["pk"]))
			sealed = crypto.seal(key, json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
				("dm|%s|%s" % (self.me, peer)).encode())
		except Exception as e:
			if cb:
				cb(str(e))
			return
		body = json.dumps({"v": 1, "f": self.me, "k": self.pk, "c": crypto.b64(sealed)}, separators=(",", ":"))
		wrapped = (lambda err: self.call(cb, err)) if cb else None
		self.sender.put(crypto.inbox_topic(peer, c["pk"]), body, cache, wrapped)

	def _send_group_event(self, chat, event, cache=True, cb=None):
		ver = str(chat["ver"])
		event = dict(event, f=self.me)
		sealed = crypto.seal(crypto.unb64(chat["keys"][ver]),
			json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), ("grp|%s|%s" % (chat["gid"], ver)).encode())
		body = json.dumps({"v": 1, "g": chat["gid"], "r": int(ver), "c": crypto.b64(sealed)}, separators=(",", ":"))
		wrapped = (lambda err: self.call(cb, err)) if cb else None
		self.sender.put(crypto.group_topic(chat["gid"]), body, cache, wrapped)

	def _chat_event(self, chat, event, cache=True, cb=None):
		if chat["type"] == "dm":
			self._send_dm_event(chat["peer"], event, cache, cb)
		elif chat["type"] == "group":
			self._send_group_event(chat, event, cache, cb)

	def _can_send(self, chat):
		if chat["type"] == "group":
			if chat.get("left") or chat.get("removed"):
				return _("You are no longer in this group.")
			return None
		if chat["type"] == "dm":
			c = self.d["contacts"].get(chat["peer"])
			if not c or not c.get("accepted"):
				return _("Accept this chat first.")
			if c.get("blocked"):
				return _("You blocked this contact. Unblock to send messages.")
			if c.get("new_pk"):
				return _("The security key of this contact changed. Open the contact info and choose to trust the new key first.")
		return None

	# ------------------------------------------------------------ sending messages
	def send_text(self, chat, text, reply=None):
		"""Send text. Returns an error text, or None."""
		text = text.strip()
		if not text:
			return None
		err = self._can_send(chat)
		if err:
			return err
		parts = [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)]
		for i, part in enumerate(parts):
			self.send_message(chat, text=part, reply=reply if i == 0 else None)
		return None

	def send_message(self, chat, text="", kind="text", att=None, poll=None, contact=None, reply=None, fwd=False):
		if chat["type"] == "broadcast":
			return self._send_broadcast(chat, text, kind, att, poll, contact)
		ts = now()
		msg = {"id": new_id(), "ts": ts, "f": self.me, "x": text, "kind": kind, "own": True, "sent": 0}
		if reply:
			msg["rp"] = {"id": reply["id"], "f": reply.get("f", ""), "x": snippet(self.msg_summary(chat, reply).split(": ", 1)[-1], 80)}
		if fwd:
			msg["fw"] = 1
		if att:
			msg["at"] = att
		if poll:
			msg["pl"] = poll
		if contact:
			msg["ct"] = contact
		if chat.get("disap"):
			msg["ex"] = ts + chat["disap"]
		self._insert(chat, msg)
		chat["archived"] = False
		chat["keep"] = True
		self._transmit(chat, msg)
		self.emit("sent", chat=chat, msg=msg)
		return msg

	def _event_of(self, chat, msg):
		ev = {"t": "m", "id": msg["id"], "ts": msg["ts"], "x": msg["x"], "kind": msg["kind"]}
		for k in ("rp", "fw", "ct"):
			if msg.get(k):
				ev[k] = msg[k]
		if msg.get("at"):
			at = dict(msg["at"])
			at.pop("path", None)
			at.pop("saved", None)
			at.pop("err", None)
			ev["at"] = at
		if msg.get("pl"):
			ev["pl"] = {"q": msg["pl"]["q"], "o": msg["pl"]["o"], "m": msg["pl"].get("m", False)}
		if chat.get("disap"):
			ev["ex"] = chat["disap"]
		return ev

	def _transmit(self, chat, msg):
		if chat["type"] == "dm" and chat.get("peer") == self.me:
			msg["sent"] = 1
			self.touch()
			if msg.get("kind") == "text":
				ev = {"t": "m", "id": new_id(), "ts": now(), "x": msg["x"], "kind": "text"}
				self._dispatch(self.me, chat, ev, now(), True)
			self.emit("receipt", chat=chat)
			return
		def done(err):
			if not self.me:
				return
			msg["sent"] = -1 if err else 1
			self.touch()
			self.emit("receipt", chat=chat)
			if err:
				self.emit("failed", chat=chat, msg=msg)
		self._chat_event(chat, self._event_of(chat, msg), True, done)

	def retry(self, chat, msg):
		if msg.get("own") and msg.get("sent") == -1:
			msg["sent"] = 0
			self._transmit(chat, msg)

	def _send_broadcast(self, chat, text, kind, att, poll, contact):
		ts = now()
		msg = {"id": new_id(), "ts": ts, "f": self.me, "x": text, "kind": kind, "own": True, "sent": 1, "bc": True}
		if att:
			msg["at"] = att
		self._insert(chat, msg)
		for u in chat["members"]:
			c = self.d["contacts"].get(u)
			if c and c.get("accepted") and not c.get("blocked"):
				self.send_message(self.dm_chat(u), text=text, kind=kind, att=att, contact=contact)
		return msg

	def send_poll(self, chat, question, options, multi=False):
		options = [snippet(o, 80) for o in options if o.strip()][:12]
		if not question.strip() or len(options) < 2:
			return _("A poll needs a question and at least two options.")
		err = self._can_send(chat)
		if err:
			return err
		self.send_message(chat, text=question, kind="poll", poll={"q": snippet(question, 200), "o": options, "m": bool(multi), "v": {}})
		return None

	def vote(self, chat, msg, idx):
		if not msg.get("pl") or msg.get("deleted"):
			return
		n = len(msg["pl"]["o"])
		idx = sorted(set(i for i in idx if 0 <= i < n))
		if not msg["pl"].get("m"):
			idx = idx[:1]
		msg["pl"].setdefault("v", {})[self.me] = idx
		self.touch()
		self._chat_event(chat, {"t": "pv", "id": msg["id"], "o": idx})
		self.emit("msgupdate", chat=chat, msg=msg, live=False, note=None)

	def share_contact(self, chat, username):
		c = self.d["contacts"].get(username)
		name = self.display_name(username) if c else username
		return self.send_message(chat, text=name, kind="contact", contact={"u": username, "n": name})

	def forward(self, msg, chats):
		sent = 0
		for chat in chats:
			if self._can_send(chat) and chat["type"] != "broadcast":
				continue
			if msg["kind"] == "poll":
				continue
			at = dict(msg["at"]) if msg.get("at") else None
			if at:
				at.pop("err", None)
			self.send_message(chat, text=msg.get("x", ""), kind=msg["kind"], att=at, contact=msg.get("ct"), fwd=True)
			sent += 1
		return sent

	def send_file(self, chat, path, kind="file", duration=0, cb=None):
		"""Encrypt a file, upload it and send the message. cb(error_or_None) runs in the main thread."""
		err = self._can_send(chat)
		if err:
			if cb:
				cb(err)
			return
		try:
			size = os.path.getsize(path)
		except OSError as e:
			if cb:
				cb(str(e))
			return
		if size > transport.MAX_FILE:
			if cb:
				cb(_("The file is too large. The limit is %s.") % fmt_size(transport.MAX_FILE))
			return
		if size == 0:
			if cb:
				cb(_("The file is empty."))
			return
		name = os.path.basename(path)

		def work():
			with open(path, "rb") as f:
				data = f.read()
			key = crypto.new_key()
			url = transport.upload(crypto.file_topic(), "m%s.bin" % new_id(), crypto.seal(key, data, b"file"))
			return {"n": name, "s": size, "u": url, "k": crypto.b64(key),
				"m": mimetypes.guess_type(name)[0] or "application/octet-stream", "d": int(duration), "path": path}

		def done(r):
			if not self.me:
				return
			if not isinstance(r, dict):
				if cb:
					cb(_("The file could not be sent: %s") % r)
				return
			self.send_message(chat, text=name, kind=kind, att=r)
			if cb:
				cb(None)
		self._bg(work, done)

	def download_attachment(self, chat, msg, cb=None):
		at = msg.get("at")
		if not at or not at.get("u"):
			return
		if at.get("path") and os.path.isfile(at["path"]):
			if cb:
				cb(None)
			return
		if at.get("busy"):
			return
		at["busy"] = True

		def work():
			blob = transport.download(at["u"])
			data = crypto.open_sealed(crypto.unb64(at["k"]), blob, b"file")
			base = re.sub(r"[^\w.\- ]", "_", at.get("n") or "file")[:80] or "file"
			path = os.path.join(self.files_dir(), "%s_%s" % (msg["id"][:6], base))
			with open(path, "wb") as f:
				f.write(data)
			return path

		def done(r):
			at.pop("busy", None)
			if self.me and not isinstance(r, Exception) and os.path.isfile(r):
				at["path"] = r
				at.pop("err", None)
				self.touch()
				self.emit("msgupdate", chat=chat, msg=msg, live=False, note=None)
				if cb:
					cb(None)
			else:
				at["err"] = _("The file is no longer available (files expire after a few hours).") if "404" in str(r) else _("Download failed: %s") % r
				self.touch()
				if cb:
					cb(at["err"])
		self._bg(work, done)

	# ------------------------------------------------------------ changing messages
	def edit_message(self, chat, msg, text):
		text = text.strip()
		if not (msg.get("own") and msg["kind"] == "text" and not msg.get("deleted")):
			return _("Only your own text messages can be edited.")
		if now() - msg["ts"] > EDIT_WINDOW:
			return _("A message can be edited for 15 minutes after it was sent.")
		if not text:
			return _("The message cannot be empty.")
		msg["x"] = text[:MAX_TEXT]
		msg["edited"] = True
		self.touch()
		self._chat_event(chat, {"t": "ed", "id": msg["id"], "x": msg["x"]})
		return None

	def delete_for_me(self, chat, msg):
		if msg in chat["msgs"]:
			chat["msgs"].remove(msg)
			self.touch()

	def delete_for_everyone(self, chat, msg):
		if not msg.get("own"):
			return _("You can only delete your own messages for everyone.")
		if now() - msg["ts"] > DELETE_WINDOW:
			return _("A message can be deleted for everyone for 2 days after it was sent.")
		self._wipe(msg)
		self.touch()
		self._chat_event(chat, {"t": "dl", "id": msg["id"]})
		return None

	def react(self, chat, msg, emoji):
		rc = msg.setdefault("rc", {})
		if emoji and rc.get(self.me) != emoji:
			rc[self.me] = emoji
		else:
			rc.pop(self.me, None)
			emoji = ""
		self.touch()
		self._chat_event(chat, {"t": "rc", "id": msg["id"], "e": emoji})

	def star(self, msg):
		msg["star"] = not msg.get("star")
		self.touch()
		return msg["star"]

	def starred(self):
		out = []
		for chat in self.d["chats"].values():
			for m in chat["msgs"]:
				if m.get("star") and not m.get("deleted"):
					out.append((chat, m))
		out.sort(key=lambda cm: -cm[1]["ts"])
		return out

	def clear_chat(self, chat):
		chat["msgs"] = []
		chat["unread"] = 0
		self.touch()

	def delete_chat(self, chat):
		if chat["type"] == "group" and not chat.get("left") and not chat.get("removed"):
			self.leave_group(chat)
		self.d["chats"].pop(chat["id"], None)
		self.viewing.discard(chat["id"])
		self._rebuild_topics()
		self.touch()
		self.emit("chats")

	def pin(self, chat, on):
		if on and sum(1 for c in self.d["chats"].values() if c.get("pinned")) >= 3:
			return _("You can pin up to 3 chats.")
		chat["pinned"] = bool(on)
		self.touch()
		return None

	def archive(self, chat, on):
		chat["archived"] = bool(on)
		if on:
			chat["pinned"] = False
		self.touch()

	def mute(self, chat, seconds):
		chat["muted"] = 0 if not seconds else (-1 if seconds < 0 else now() + seconds)
		self.touch()

	def mark_unread(self, chat):
		chat["unread"] = max(1, chat.get("unread", 0))
		self.touch()

	def set_disappearing(self, chat, seconds):
		if chat["type"] == "group" and self.me not in chat["admins"]:
			return _("Only group admins can change this.")
		if chat["type"] == "broadcast":
			return None
		chat["disap"] = int(seconds)
		self._sys(chat, self._disap_note(self.me, int(seconds)))
		self._chat_event(chat, {"t": "ds", "s": int(seconds)})
		return None

	# ------------------------------------------------------------ receipts, reading, typing
	def set_viewing(self, chat, on):
		if on:
			self.viewing.add(chat["id"])
			self.mark_read(chat)
		else:
			self.viewing.discard(chat["id"])

	def mark_read(self, chat):
		chat["unread"] = 0
		ids = {}
		for m in chat["msgs"]:
			if not m.get("own") and m.get("unr"):
				m["unr"] = False
				if m["kind"] != "sys":
					ids.setdefault(m["f"], []).append(m["id"])
		self.touch()
		if chat["type"] == "dm" and chat.get("request"):
			return
		if self.setting("read_receipts"):
			for u, lst in ids.items():
				for mid in lst:
					self._queue_ack(u, chat, mid, 3)

	def _queue_ack(self, peer, chat, mid, status):
		if peer == self.me:
			return
		c = self.d["contacts"].get(peer)
		if not c or c.get("blocked") or not c.get("pk"):
			return
		gid = chat.get("gid") if chat["type"] == "group" else None
		self.ack_q.setdefault((peer, gid, status), []).append(mid)

	def _flush_acks(self):
		q, self.ack_q = self.ack_q, {}
		for (peer, gid, status), ids in q.items():
			for i in range(0, len(ids), 40):
				ev = {"t": "a", "ids": ids[i:i + 40], "s": status}
				if gid:
					ev["g"] = gid
				self._send_dm_event(peer, ev, cache=True)

	def typing_ping(self, chat):
		if not self.setting("typing") or chat["type"] == "broadcast" or self._can_send(chat):
			return
		t = time.time()
		if t - self.typing_sent.get(chat["id"], 0) < 4:
			return
		self.typing_sent[chat["id"]] = t
		self._chat_event(chat, {"t": "ty", "on": 1}, cache=False)

	def typing_text(self, chat):
		names = [self.display_name(u) for u, until in self.typing.get(chat["id"], {}).items() if until > time.time()]
		if not names:
			return ""
		return _("%s is typing") % names[0] if len(names) == 1 else _("%s are typing") % ", ".join(names)

	# ------------------------------------------------------------ presence and profile
	def is_online(self, u):
		c = self.d["contacts"].get(u)
		return bool(c and c.get("on") and c.get("ps") and time.time() - c.get("pr_ts", 0) < ONLINE_WINDOW)

	def presence_text(self, u):
		c = self.d["contacts"].get(u)
		if not c:
			return ""
		if self.is_online(u):
			return _("online")
		if c.get("ps") and c.get("seen"):
			return _("last seen %s") % fmt_time(c["seen"])
		return ""

	def presence_detail(self, u):
		"""Longer status text: online since when, or when the person was last seen."""
		c = self.d["contacts"].get(u)
		name = self.display_name(u)
		if not c or not c.get("ps"):
			return _("%s does not share online status.") % name
		if self.is_online(u):
			since = c.get("on_since")
			if since:
				return _("%s is online. Came online at %s.") % (name, fmt_time(since))
			return _("%s is online.") % name
		if c.get("seen"):
			return _("%s is offline. Last seen %s.") % (name, fmt_time(c["seen"]))
		return _("%s is offline.") % name

	def _pres_key(self):
		return crypto.hkdf(crypto.unb64(self.d["acct"]["ps"]), b"", b"myChat presence", 64)

	def _beat(self, on):
		if not self.me or not self.sender:
			return
		if not self.setting("show_online"):
			return
		sealed = crypto.seal(self._pres_key(), json.dumps({"on": 1 if on else 0, "ts": now()}).encode(), b"pres")
		self.sender.put(crypto.presence_topic(self.d["acct"]["ps"]), json.dumps({"v": 1, "c": crypto.b64(sealed)}), False, None)

	def _profile_event(self):
		return {"t": "pf", "id": new_id(), "n": self.d["acct"]["name"], "a": self.d["acct"]["about"],
			"ps": self.d["acct"]["ps"] if self.setting("show_online") else ""}

	def _send_profile(self, u):
		self._send_dm_event(u, self._profile_event())

	def _share_profile_all(self):
		for u, c in self.d["contacts"].items():
			if c.get("accepted") and not c.get("blocked") and not c.get("stub"):
				self._send_profile(u)

	def set_profile(self, name, about):
		self.d["acct"]["name"] = snippet(name, 40) or self.me
		self.d["acct"]["about"] = snippet(about, 140)
		self.touch()
		self._share_profile_all()

	# ------------------------------------------------------------ groups
	def create_group(self, name, about, members):
		name = snippet(name, 40)
		if not name:
			return None, _("Please type a group name.")
		members = [u for u in members if u in self.d["contacts"] and self.d["contacts"][u].get("accepted")]
		if not members:
			return None, _("Choose at least one friend for the group.")
		gid = os.urandom(8).hex()
		chat = self._blank_chat("g:" + gid, "group")
		chat.update({"gid": gid, "name": name, "about": snippet(about, 200), "members": [self.me] + members[:255],
			"admins": [self.me], "ver": 1, "keys": {"1": crypto.b64(crypto.new_key())}, "creator": self.me,
			"left": False, "removed": False, "keep": True})
		self.d["chats"][chat["id"]] = chat
		self._sys(chat, _("You created the group %s") % name)
		self._group_push(chat, rotate=False)
		self._rebuild_topics()
		self.touch()
		self.emit("chats")
		return chat, None

	def _group_push(self, chat, rotate=True, removed=()):
		"""Send the current group details (and a new key) privately to every member."""
		if rotate:
			chat["ver"] += 1
			chat["keys"][str(chat["ver"])] = crypto.b64(crypto.new_key())
		pks = {u: self.d["contacts"][u]["pk"] for u in chat["members"] if u in self.d["contacts"] and self.d["contacts"][u].get("pk")}
		pks[self.me] = self.pk
		ev = {"t": "gi", "id": new_id(), "gid": chat["gid"], "n": chat["name"], "ab": chat["about"],
			"mem": chat["members"], "adm": chat["admins"], "ver": chat["ver"], "key": chat["keys"][str(chat["ver"])], "pks": pks}
		for u in chat["members"]:
			if u != self.me:
				self._send_dm_event(u, ev)
		for u in removed:
			self._send_dm_event(u, dict(ev, key="x"))
		self.touch()

	def _is_admin(self, chat):
		return self.me in chat.get("admins", [])

	def group_add(self, chat, users):
		if not self._is_admin(chat):
			return _("Only group admins can add people.")
		new = [u for u in users if u not in chat["members"] and u in self.d["contacts"] and self.d["contacts"][u].get("accepted")]
		if not new:
			return _("Nobody to add.")
		chat["members"].extend(new)
		for u in new:
			self._sys(chat, _("You added %s") % self.display_name(u))
		self._group_push(chat)
		self._rebuild_topics()
		return None

	def group_remove(self, chat, user):
		if not self._is_admin(chat):
			return _("Only group admins can remove people.")
		if user == self.me or user not in chat["members"]:
			return _("Choose another member.")
		chat["members"].remove(user)
		if user in chat["admins"]:
			chat["admins"].remove(user)
		self._sys(chat, _("You removed %s") % self.display_name(user))
		self._group_push(chat, removed=[user])
		return None

	def group_set_admin(self, chat, user, on):
		if not self._is_admin(chat):
			return _("Only group admins can do this.")
		if on and user not in chat["admins"]:
			chat["admins"].append(user)
		elif not on and user in chat["admins"]:
			if len(chat["admins"]) == 1:
				return _("A group needs at least one admin.")
			chat["admins"].remove(user)
		self._group_push(chat, rotate=False)
		return None

	def group_edit(self, chat, name, about):
		if not self._is_admin(chat):
			return _("Only group admins can change the group details.")
		chat["name"] = snippet(name, 40) or chat["name"]
		chat["about"] = snippet(about, 200)
		self._group_push(chat, rotate=False)
		return None

	def leave_group(self, chat):
		if chat.get("left") or chat.get("removed"):
			return
		self._send_group_event(chat, {"t": "gl"})
		chat["left"] = True
		self._sys(chat, _("You left the group"))
		self._rebuild_topics()
		self.touch()

	# ------------------------------------------------------------ broadcast lists
	def create_broadcast(self, name, members):
		members = [u for u in members if u in self.d["contacts"] and self.d["contacts"][u].get("accepted")]
		if not members:
			return None, _("Choose at least one friend.")
		chat = self._blank_chat("b:" + new_id(), "broadcast")
		chat.update({"name": snippet(name, 40) or _("Broadcast list"), "members": members, "keep": True})
		self.d["chats"][chat["id"]] = chat
		self.touch()
		self.emit("chats")
		return chat, None

	# ------------------------------------------------------------ status (stories)
	def post_status(self, text):
		text = text.strip()[:MAX_TEXT]
		if not text:
			return
		sid = new_id()
		self.d["status"].setdefault(self.me, []).append({"id": sid, "x": text, "ts": now()})
		for u, c in self.d["contacts"].items():
			if c.get("accepted") and not c.get("blocked") and not c.get("stub"):
				self._send_dm_event(u, {"t": "st", "id": sid, "x": text})
		self.touch()

	def delete_status(self, sid):
		self.d["status"][self.me] = [s for s in self.d["status"].get(self.me, []) if s["id"] != sid]
		for u, c in self.d["contacts"].items():
			if c.get("accepted") and not c.get("blocked") and not c.get("stub"):
				self._send_dm_event(u, {"t": "sd", "id": sid})
		self.touch()

	def status_list(self):
		"""[(user, [status...])] of friends with a status, newest first, and my own status."""
		out = []
		for u, lst in self.d["status"].items():
			if lst and (u == self.me or (u in self.d["contacts"] and not self.d["contacts"][u].get("blocked"))):
				out.append((u, sorted(lst, key=lambda s: s["ts"])))
		out.sort(key=lambda p: (p[0] != self.me, -p[1][-1]["ts"]))
		return out

	# ------------------------------------------------------------ search
	def search_chat(self, chat, q):
		q = q.lower().strip()
		return [m for m in chat["msgs"] if q and not m.get("deleted") and m["kind"] != "sys"
			and (q in (m.get("x") or "").lower() or q in (m.get("at", {}).get("n", "")).lower())]

	def search_all(self, q):
		out = []
		for chat in self.d["chats"].values():
			for m in self.search_chat(chat, q):
				out.append((chat, m))
		out.sort(key=lambda cm: -cm[1]["ts"])
		return out[:200]

	# ------------------------------------------------------------ text for the screen
	def msg_status(self, chat, m):
		if not m.get("own"):
			return ""
		if m.get("sent") == -1:
			return _("not sent")
		if not m.get("sent"):
			return _("sending")
		if chat["type"] == "broadcast":
			return _("sent")
		who = [u for u in (chat["members"] if chat["type"] == "group" else [chat.get("peer")]) if u and u != self.me]
		if not who:
			return _("sent")
		rd, dl = m.get("rd", {}), m.get("dl", {})
		if all(u in rd for u in who):
			return _("read")
		if all(u in dl or u in rd for u in who):
			return _("delivered")
		return _("sent")

	def format_message(self, chat, m):
		ts = fmt_time(m["ts"])
		if m["kind"] == "sys":
			return "[%s] %s" % (ts, m["x"])
		sender = _("You") if m.get("own") else self.display_name(m.get("f", ""))
		if m.get("deleted"):
			body = _("This message was deleted") if not m.get("own") else _("You deleted this message")
		else:
			k = m["kind"]
			if k == "voice":
				d = m.get("at", {}).get("d", 0)
				body = _("Voice message %d:%02d") % (d // 60, d % 60) + self._att_state(m)
			elif k == "file":
				at = m.get("at", {})
				body = _("File: %s (%s)") % (at.get("n", ""), fmt_size(at.get("s", 0))) + self._att_state(m)
			elif k == "poll":
				body = self._poll_text(m)
			elif k == "contact":
				body = _("Contact: %s (user name %s)") % (m.get("ct", {}).get("n", ""), m.get("ct", {}).get("u", ""))
			else:
				body = m.get("x", "")
			if m.get("rp"):
				body = _("Reply to %s: “%s”. %s") % (self.display_name(m["rp"].get("f", "")) if m["rp"].get("f") != self.me else _("You"), m["rp"].get("x", ""), body)
			if m.get("fw"):
				body = _("Forwarded: %s") % body
			if m.get("edited"):
				body += " " + _("(edited)")
		line = "[%s] %s: %s" % (ts, sender, body)
		if m.get("star"):
			line += " ★"
		rc = m.get("rc")
		if rc:
			line += " " + _("Reactions:") + " " + ", ".join("%s %s" % (e, self.display_name(u)) for u, e in rc.items())
		st = self.msg_status(chat, m)
		if st:
			line += " (%s)" % st
		if m.get("ex"):
			line += " " + _("[disappears]")
		return line

	def _att_state(self, m):
		at = m.get("at", {})
		if at.get("path") and os.path.isfile(at["path"]):
			return ""
		if at.get("err"):
			return " - " + at["err"]
		if at.get("busy"):
			return " - " + _("downloading")
		return " - " + _("not downloaded")

	def _poll_text(self, m):
		pl = m.get("pl", {})
		votes = pl.get("v", {})
		counts = [0] * len(pl.get("o", []))
		for u, idx in votes.items():
			for i in idx:
				if 0 <= i < len(counts):
					counts[i] += 1
		parts = ["%d. %s (%d)" % (i + 1, o, counts[i]) for i, o in enumerate(pl.get("o", []))]
		mine = votes.get(self.me)
		extra = (" " + _("You voted: %s") % ", ".join(str(i + 1) for i in mine)) if mine else ""
		return _("Poll: %s. Options: %s.") % (pl.get("q", ""), "; ".join(parts)) + extra

	def message_info(self, chat, m):
		lines = [_("Sent: %s") % fmt_time(m["ts"])]
		if m.get("own"):
			who = [u for u in (chat["members"] if chat["type"] == "group" else [chat.get("peer")]) if u and u != self.me]
			for u in who:
				rd, dl = m.get("rd", {}), m.get("dl", {})
				if u in rd:
					state = _("read at %s") % fmt_time(rd[u])
				elif u in dl:
					state = _("delivered at %s") % fmt_time(dl[u])
				else:
					state = _("not delivered yet")
				lines.append("%s: %s" % (self.display_name(u), state))
		if m.get("ex"):
			lines.append(_("Disappears at %s") % fmt_time(m["ex"]))
		return "\n".join(lines)

	def contact_info(self, u):
		c = self.d["contacts"].get(u)
		if not c:
			return ""
		lines = [_("Name: %s") % (c.get("name") or u), _("User name: %s") % u]
		if c.get("nick"):
			lines.append(_("Your name for this contact: %s") % c["nick"])
		if c.get("about"):
			lines.append(_("About: %s") % c["about"])
		p = self.presence_text(u)
		if p:
			lines.append(p.capitalize())
		if c.get("blocked"):
			lines.append(_("Blocked"))
		if c.get("suspicious"):
			lines.append(_("Warning: the key of this person does not match the public entry of this user name."))
		if c.get("new_pk"):
			lines.append(_("Warning: the security key of this contact changed."))
		lines.append(_("Safety number (compare it with your friend, for example by voice, to be sure that nobody listens in):"))
		lines.append(self.safety_number(u))
		return "\n".join(lines)

	def group_info(self, chat):
		lines = [_("Group: %s") % chat["name"]]
		if chat.get("about"):
			lines.append(_("Description: %s") % chat["about"])
		lines.append(_("Created by: %s") % self.display_name(chat.get("creator", "")))
		lines.append(_("%d members:") % len(chat["members"]))
		for u in chat["members"]:
			tag = _(" (admin)") if u in chat["admins"] else ""
			lines.append("%s%s" % (self.display_name(u) if u != self.me else _("You"), tag))
		if chat.get("disap"):
			lines.append(_("Disappearing messages: %s") % dict(DISAPPEAR).get(chat["disap"], chat["disap"]))
		return "\n".join(lines)

	def export_chat(self, chat):
		return "\n".join(self.format_message(chat, m) for m in chat["msgs"])

	# ------------------------------------------------------------ the clock
	def _publish_directory(self):
		rec = json.dumps({"u": self.me, "k": self.pk, "v": 1})
		self.sender.put(crypto.directory_topic(self.me), rec, True, None)
		self.last_dir = now()

	def _tick(self, gen):
		if gen != self.gen or not self.me:
			return
		t = now()
		if self.ack_q:
			self._flush_acks()
		if t - self.last_ping >= PING_EVERY:
			self.last_ping = t
			self._beat(True)
		if t - self.last_dir >= 3 * 3600:
			self._publish_directory()
		if t - self.last_clean >= 10:
			self.last_clean = t
			self._cleanup(t)
		for cid, users in list(self.typing.items()):
			gone = [u for u, until in users.items() if until < time.time()]
			for u in gone:
				users.pop(u, None)
			if gone and cid in self.d["chats"]:
				self.emit("typing", chat=self.d["chats"][cid], text=self.typing_text(self.d["chats"][cid]))
		for c in self.d["contacts"].values():
			if c.get("on") and time.time() - c.get("pr_ts", 0) >= ONLINE_WINDOW:
				c["on"] = False
				self.emit("presence", user=c["u"], online=False)
		if not self.catchup_done and t - self.conn_time >= 6 and self.connected:
			self.catchup_done = True
			self.emit("catchup", unread=self.unread_total(), chats=[c for c in self.d["chats"].values() if c.get("unread") and not self.is_muted(c)])
		if self.dirty and t - self.last_save >= 3:
			self.last_save = t
			self.save()

	def _cleanup(self, t):
		changed = False
		for chat in self.d["chats"].values():
			keep = [m for m in chat["msgs"] if not (m.get("ex") and m["ex"] <= t)]
			if len(keep) != len(chat["msgs"]):
				chat["msgs"] = keep
				changed = True
				self.emit("msgupdate", chat=chat, msg=None, live=False, note=None)
		for u in list(self.d["status"]):
			lst = [s for s in self.d["status"][u] if t - s["ts"] < STATUS_LIFE]
			if len(lst) != len(self.d["status"][u]):
				self.d["status"][u] = lst
				changed = True
		if changed:
			self.touch()
