# -*- coding: UTF-8 -*-
"""Friends Mirror extras: AI tools, scheduled messages, reminders, quick replies, auto reply, quiet hours,
auto translation, keyword alerts, app lock, statistics, backup and more."""
import base64
import collections
import datetime
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import wx
import gui
import api
import addonHandler
from gui import guiHelper
from logHandler import log
from .uihelp import speak, _finish, _modal, ask_text, ask_form, choose, confirm, show_text
from . import sounds

addonHandler.initTranslation()

PROVIDERS = [
	("Google Gemini", "gemini", "", "gemini-2.5-flash"),
	("OpenAI", "openai", "https://api.openai.com/v1", "gpt-4o-mini"),
	("Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
	("OpenRouter", "openai", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
	("Custom (OpenAI compatible)", "openai", "", ""),
]
LANGS = ["English", "Tamil", "Hindi", "Telugu", "Malayalam", "Kannada", "Bengali", "Urdu", "Arabic", "French", "German", "Spanish",
	"Portuguese", "Italian", "Russian", "Chinese", "Japanese", "Korean", "Turkish", "Indonesian", "Dutch", "Swahili"]
DEFAULT_QUICK = ["Thank you!", "OK", "On my way.", "I will call you back later.", "Can we talk later?", "Good morning!", "Good night!"]
TONES = [
	(_("Polite and respectful"), "Rewrite the message so it is polite and respectful."),
	(_("Friendly and warm"), "Rewrite the message so it sounds friendly and warm."),
	(_("Formal"), "Rewrite the message in a formal tone."),
	(_("Shorter"), "Make the message shorter and keep the meaning."),
	(_("Longer, with more detail"), "Expand the message with a little more detail and keep the meaning."),
	(_("Fix spelling and grammar"), "Fix all spelling and grammar mistakes. Change nothing else."),
	(_("Funny"), "Rewrite the message with light, friendly humour."),
	(_("Confident"), "Rewrite the message so it sounds confident and clear."),
	(_("Translate to my language"), "TRANSLATE_MY"),
	(_("Translate to another language..."), "TRANSLATE_ASK"),
]
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


# ---------------------------------------------------------------- storage
def X(e):
	"""The extras of the signed-in account, saved with the account."""
	x = e.d.setdefault("extras", {})
	x.setdefault("quick", list(DEFAULT_QUICK))
	x.setdefault("sched", [])
	x.setdefault("rem", [])
	x.setdefault("auto", {"on": False, "text": "I am not available right now. I will reply later.", "log": {}})
	x.setdefault("quiet", {"on": False, "from": 22, "to": 7})
	x.setdefault("autotr", False)
	x.setdefault("keywords", [])
	x.setdefault("lang", "English")
	x.setdefault("ai", {"provider": 0, "key": "", "model": "", "base": ""})
	return x


def changed(e):
	e.touch()
	try:
		e.save(False)
	except Exception:
		pass


# ---------------------------------------------------------------- AI engine
def _post_json(url, body, headers, timeout=90):
	req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
		headers=dict({"Content-Type": "application/json", "User-Agent": "NVDA-FriendsMirror/1.0"}, **headers), method="POST")
	try:
		with urllib.request.urlopen(req, timeout=timeout) as r:
			return json.loads(r.read().decode("utf-8"))
	except urllib.error.HTTPError as ex:
		try:
			msg = json.loads(ex.read().decode("utf-8"))
			msg = msg.get("error", msg)
			msg = msg.get("message", msg) if isinstance(msg, dict) else msg
		except Exception:
			msg = ex.reason
		raise RuntimeError("%s %s" % (ex.code, msg))


def ai_config(e):
	a = X(e)["ai"]
	name, kind, base, model = PROVIDERS[max(0, min(int(a.get("provider", 0)), len(PROVIDERS) - 1))]
	return {"kind": kind, "key": a.get("key", "").strip(), "base": (a.get("base") or base).rstrip("/"),
		"model": a.get("model") or model, "name": name}


SYSTEM = ("You are a helpful assistant inside a chat app used by blind people with a screen reader. Reply in plain text only, "
	"with no markdown symbols (no asterisks, hashes or backticks). Be clear and short.")


def ask_ai(e, prompt, system=SYSTEM, history=None, media=None):
	"""Blocking AI call. media is (mime, bytes) for Gemini pictures and audio. Raises RuntimeError with a readable message."""
	c = ai_config(e)
	if not c["key"]:
		raise RuntimeError(_("Please set your AI provider and API key first: Settings, AI settings."))
	if c["kind"] == "gemini":
		parts = []
		if media:
			parts.append({"inline_data": {"mime_type": media[0], "data": base64.b64encode(media[1]).decode("ascii")}})
		contents = []
		for h in history or []:
			contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["content"]}]})
		contents.append({"role": "user", "parts": parts + [{"text": prompt}]})
		data = _post_json("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % c["model"],
			{"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}, {"x-goog-api-key": c["key"]})
		try:
			return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
		except Exception:
			raise RuntimeError(_("The AI gave no answer."))
	if media:
		raise RuntimeError(_("Pictures and audio need Google Gemini. Choose Google Gemini in Settings, AI settings."))
	if not c["base"]:
		raise RuntimeError(_("Please type the base URL of your AI provider in Settings, AI settings."))
	msgs = [{"role": "system", "content": system}] + list(history or []) + [{"role": "user", "content": prompt}]
	data = _post_json(c["base"] + "/chat/completions", {"model": c["model"], "messages": msgs}, {"Authorization": "Bearer " + c["key"]})
	try:
		return data["choices"][0]["message"]["content"].strip()
	except Exception:
		raise RuntimeError(_("The AI gave no answer."))


def run_ai(ctrl, title, prompt, done=None, system=SYSTEM, media=None, history=None):
	"""Runs the AI in the background. done(text) runs in the main thread; by default the answer opens in a result box."""
	e = ctrl.engine
	speak(_("Please wait"))
	sounds.play("working")

	def work():
		try:
			r = ask_ai(e, prompt, system, history, media)
			err = None
		except Exception as ex:
			r, err = None, str(ex)
		wx.CallAfter(finish, r, err)

	def finish(r, err):
		if err:
			sounds.play("error")
			show_text(title, _("Error: %s") % err)
			return
		sounds.play("success")
		(done or (lambda t: show_text(title, t)))(r)
	threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- chat text helpers
def clock(ts):
	return time.strftime("%d %b %H:%M", time.localtime(ts))


def transcript(e, chat, n=80, unread=False):
	msgs = [m for m in chat["msgs"] if m["kind"] != "sys" and not m.get("deleted")]
	if unread:
		msgs = msgs[-max(0, chat.get("unread", 0)):] if chat.get("unread") else []
	lines = []
	for m in msgs[-n:]:
		who = _("Me") if m.get("own") else e.display_name(m.get("f", ""))
		text = m.get("x") if m["kind"] == "text" else e.msg_summary(chat, m).split(": ", 1)[-1]
		lines.append("[%s] %s: %s" % (clock(m["ts"]), who, text))
	return "\n".join(lines)


def msg_text(e, chat, m):
	return m.get("x") if m["kind"] == "text" else e.msg_summary(chat, m).split(": ", 1)[-1]


def target_lang(e):
	return X(e)["lang"]


# ---------------------------------------------------------------- tools inside a chat
def smart_replies(ctrl, chat, window, m=None):
	e = ctrl.engine
	base = transcript(e, chat, 12)
	if not base:
		speak(_("There are no messages to reply to."))
		return
	focus = ("\nReply especially to this message: %s" % msg_text(e, chat, m)) if m else ""
	prompt = ("Here is a chat. Suggest 4 short, natural replies that the user (Me) could send next. "
		"Answer with the 4 replies only, one on each line, with no numbers.%s\n\n%s" % (focus, base))

	def done(text):
		opts = [re.sub(r"^[\-\*\d\.\)\s]+", "", l).strip() for l in text.splitlines() if l.strip()][:5]
		if not opts:
			show_text(_("Smart replies"), text)
			return
		i = choose(_("&Choose a reply to put in your message box:"), _("Smart replies"), opts)
		if i >= 0 and window:
			window.input.SetValue(opts[i])
			window.input.SetInsertionPointEnd()
			window.input.SetFocus()
	run_ai(ctrl, _("Smart replies"), prompt, done)


def rewrite(ctrl, chat, window):
	e = ctrl.engine
	text = window.input.GetValue().strip() if window else ""
	if not text:
		speak(_("Type your message first, then use this tool to improve it."))
		return
	i = choose(_("&How should the AI change your message:"), _("Rewrite my message"), [t[0] for t in TONES])
	if i < 0:
		return
	instr = TONES[i][1]
	if instr == "TRANSLATE_MY":
		instr = "Translate the message into %s." % target_lang(e)
	elif instr == "TRANSLATE_ASK":
		j = choose(_("&Translate into:"), _("Translate my message"), LANGS)
		if j < 0:
			return
		instr = "Translate the message into %s." % LANGS[j]
	prompt = "%s Reply with only the new message, nothing else.\n\nMessage:\n%s" % (instr, text)

	def done(t):
		window.input.SetValue(t.strip())
		window.input.SetInsertionPointEnd()
		window.input.SetFocus()
		speak(_("Your message was changed. It has not been sent. %s") % t)
	run_ai(ctrl, _("Rewrite my message"), prompt, done)


def draft(ctrl, chat, window):
	idea = ask_text(_("What do you want to say? Describe it in a few words:"), _("Write a message with AI"))
	if not idea:
		return
	ctx = transcript(ctrl.engine, chat, 8)
	prompt = ("Write a short chat message for the user (Me) based on this idea: %s\nReply with only the message.%s"
		% (idea, ("\n\nRecent chat for context:\n" + ctx) if ctx else ""))

	def done(t):
		window.input.SetValue(t.strip())
		window.input.SetInsertionPointEnd()
		window.input.SetFocus()
		speak(_("Message written. It has not been sent. %s") % t)
	run_ai(ctrl, _("Write a message with AI"), prompt, done)


def summarize(ctrl, chat, window=None):
	t = transcript(ctrl.engine, chat, 200)
	if not t:
		speak(_("This chat has no messages."))
		return
	run_ai(ctrl, _("Chat summary"), "Summarize this chat in a few short paragraphs. Mention decisions, questions and anything I must do.\n\n" + t)


def catch_up(ctrl, chat, window=None):
	t = transcript(ctrl.engine, chat, 100, unread=True)
	if not t:
		speak(_("You have no unread messages in this chat."))
		return
	run_ai(ctrl, _("Catch up"), "I missed these chat messages. Tell me briefly what was said and what needs a reply.\n\n" + t)


def ask_about_chat(ctrl, chat, window=None):
	q = ask_text(_("What do you want to know about this chat?"), _("Ask AI about this chat"))
	if not q:
		return
	t = transcript(ctrl.engine, chat, 300)
	run_ai(ctrl, _("Answer"), "Answer the question using only this chat.\nQuestion: %s\n\nChat:\n%s" % (q, t))


def translate_message(ctrl, chat, window, m):
	e = ctrl.engine
	text = msg_text(e, chat, m)
	run_ai(ctrl, _("Translation"), "Translate this message into %s. Reply with only the translation.\n\n%s" % (target_lang(e), text))


def explain_message(ctrl, chat, window, m):
	e = ctrl.engine
	ctx = transcript(e, chat, 6)
	run_ai(ctrl, _("Explanation"), "Explain what this chat message means in simple words, including any slang, short forms or emojis.\nMessage: %s\n\nRecent chat:\n%s"
		% (msg_text(e, chat, m), ctx))


def _local_file(ctrl, chat, window, m, then):
	at = m.get("at", {})
	if at.get("path") and os.path.isfile(at["path"]):
		then(at["path"])
		return
	speak(_("Downloading"))

	def done(err):
		if err:
			speak(err)
		else:
			then(m["at"]["path"])
	ctrl.engine.download_attachment(chat, m, done)


def describe_picture(ctrl, chat, window, m):
	def go(path):
		mime = mimetypes.guess_type(path)[0] or "image/jpeg"
		if not mime.startswith("image/"):
			show_text(_("Describe picture"), _("This file is not a picture."))
			return
		with open(path, "rb") as f:
			data = f.read()
		run_ai(ctrl, _("Picture description"), "Describe this picture in detail for a blind person: the scene, people, objects, colors and any text. "
			"Answer in %s." % target_lang(ctrl.engine), media=(mime, data))
	_local_file(ctrl, chat, window, m, go)


def transcribe_voice(ctrl, chat, window, m):
	def go(path):
		with open(path, "rb") as f:
			data = f.read()
		mime = mimetypes.guess_type(path)[0] or "audio/wav"
		run_ai(ctrl, _("Voice message text"), "Write down exactly what is said in this audio. If it is not in %s, add a translation into %s after the text."
			% (target_lang(ctrl.engine), target_lang(ctrl.engine)), media=(mime, data))
	_local_file(ctrl, chat, window, m, go)


def read_aloud(ctrl, chat, window, m):
	speak(msg_text(ctrl.engine, chat, m))


def links_of(e, chat, m):
	return URL_RE.findall(msg_text(e, chat, m) or "")


def open_link(ctrl, chat, window, m):
	links = links_of(ctrl.engine, chat, m)
	if not links:
		return
	i = 0 if len(links) == 1 else choose(_("&Link to open:"), _("Open link"), links)
	if i >= 0:
		webbrowser.open(links[i])


def summarize_link(ctrl, chat, window, m):
	links = links_of(ctrl.engine, chat, m)
	i = 0 if len(links) == 1 else choose(_("&Link to summarize:"), _("Summarize link"), links)
	if i < 0:
		return
	url = links[i]
	e = ctrl.engine
	speak(_("Please wait"))

	def work():
		try:
			req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
			with urllib.request.urlopen(req, timeout=20) as r:
				html = r.read(600000).decode("utf-8", "replace")
			text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
			text = re.sub(r"<[^>]+>", " ", text)
			text = re.sub(r"\s+", " ", text)[:12000]
			out = ask_ai(e, "Summarize this web page in a few short paragraphs:\n\n" + text)
			err = None
		except Exception as ex:
			out, err = None, str(ex)
		wx.CallAfter(lambda: show_text(_("Link summary"), out if not err else _("Error: %s") % err))
	threading.Thread(target=work, daemon=True).start()


def stats(ctrl, chat, window=None):
	e = ctrl.engine
	msgs = [m for m in chat["msgs"] if m["kind"] != "sys"]
	if not msgs:
		speak(_("This chat has no messages."))
		return
	who = collections.Counter((_("You") if m.get("own") else e.display_name(m.get("f", ""))) for m in msgs)
	hours = collections.Counter(time.localtime(m["ts"]).tm_hour for m in msgs)
	days = collections.Counter(time.strftime("%Y-%m-%d", time.localtime(m["ts"])) for m in msgs)
	words = sum(len((m.get("x") or "").split()) for m in msgs if m["kind"] == "text")
	kinds = collections.Counter(m["kind"] for m in msgs)
	busy_h, _n = hours.most_common(1)[0]
	busy_d, bn = days.most_common(1)[0]
	lines = [_("%d messages and %d words from %s to %s.") % (len(msgs), words, clock(msgs[0]["ts"]), clock(msgs[-1]["ts"])), "",
		_("Messages by person:")] + ["%s: %d" % kv for kv in who.most_common()]
	lines += ["", _("Busiest hour: %02d:00. Busiest day: %s with %d messages.") % (busy_h, busy_d, bn),
		_("Average per active day: %.1f messages.") % (len(msgs) / float(len(days))),
		_("Types: %s") % ", ".join("%s %d" % kv for kv in kinds.most_common())]
	show_text(_("Chat statistics"), "\n".join(lines))


def read_last(ctrl, chat, window=None):
	e = ctrl.engine
	i = choose(_("&How many of the last messages:"), _("Read last messages"), ["3", "5", "10", "20"])
	if i < 0:
		return
	n = [3, 5, 10, 20][i]
	t = transcript(e, chat, n)
	speak(t or _("This chat has no messages."))


# ---------------------------------------------------------------- scheduled messages and reminders
WHEN = [_("In 10 minutes"), _("In 30 minutes"), _("In 1 hour"), _("In 3 hours"), _("Tomorrow at 9:00"), _("Tomorrow at 18:00"),
	_("At a date and time I type below")]
WHEN_SEC = [600, 1800, 3600, 10800]


def _ask_when(title):
	now = datetime.datetime.now()

	def check(v):
		if v["w_index"] == 6:
			try:
				datetime.datetime.strptime(v["at"], "%Y-%m-%d %H:%M")
			except ValueError:
				return _("Type the date and time like 2026-12-31 18:30.")
		return None
	r = ask_form(title, [
		{"key": "w", "label": _("&When:"), "type": "combo", "choices": WHEN},
		{"key": "at", "label": _("&Date and time (only for the last choice), like 2026-12-31 18:30:"),
			"default": (now + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")},
	], validate=check)
	if not r:
		return None
	i = r["w_index"]
	if i < 4:
		return time.time() + WHEN_SEC[i]
	if i == 4 or i == 5:
		d = (now + datetime.timedelta(days=1)).replace(hour=9 if i == 4 else 18, minute=0, second=0, microsecond=0)
		return d.timestamp()
	return datetime.datetime.strptime(r["at"], "%Y-%m-%d %H:%M").timestamp()


def schedule_message(ctrl, chat, window=None, text=""):
	e = ctrl.engine
	if window and not text:
		text = window.input.GetValue().strip()
	text = ask_text(_("Message to send later:"), _("Schedule a message"), text, multiline=True)
	if not text:
		return
	ts = _ask_when(_("Schedule a message"))
	if not ts:
		return
	X(e)["sched"].append({"id": os.urandom(4).hex(), "chat": chat["id"], "text": text, "ts": ts})
	changed(e)
	if window:
		window.input.SetValue("")
	speak(_("Scheduled for %s") % clock(ts))


def manage_scheduled(ctrl):
	e = ctrl.engine
	while True:
		items = sorted(X(e)["sched"], key=lambda s: s["ts"])
		if not items:
			speak(_("You have no scheduled messages."))
			return
		labels = ["%s, to %s: %s" % (clock(s["ts"]), _chat_name(e, s["chat"]), s["text"][:60]) for s in items]
		i = choose(_("&Scheduled messages (choose one to delete):"), _("Scheduled messages"), labels)
		if i < 0:
			return
		if confirm(_("Delete this scheduled message?\n%s") % labels[i], _("Scheduled messages")):
			X(e)["sched"].remove(items[i])
			changed(e)
			speak(_("Deleted"))


def _chat_name(e, cid):
	c = e.d["chats"].get(cid)
	return e.chat_title(c) if c else _("a deleted chat")


def remind_me(ctrl, chat, window, m):
	e = ctrl.engine
	ts = _ask_when(_("Remind me about this message"))
	if not ts:
		return
	X(e)["rem"].append({"ts": ts, "text": _("Reminder about a message from %s in %s: %s") % (
		e.display_name(m.get("f", "")) if not m.get("own") else _("you"), e.chat_title(chat), msg_text(e, chat, m)[:200])})
	changed(e)
	speak(_("I will remind you at %s") % clock(ts))


def new_reminder(ctrl):
	e = ctrl.engine
	t = ask_text(_("What should I remind you about?"), _("New reminder"))
	if not t:
		return
	ts = _ask_when(_("New reminder"))
	if ts:
		X(e)["rem"].append({"ts": ts, "text": t})
		changed(e)
		speak(_("I will remind you at %s") % clock(ts))


def tick(ctrl):
	"""Runs every few seconds: sends due scheduled messages and speaks due reminders."""
	e = ctrl.engine
	if not e.me or e.d is None:
		return
	x = X(e)
	t = time.time()
	for s in [s for s in x["sched"] if s["ts"] <= t]:
		chat = e.d["chats"].get(s["chat"])
		x["sched"].remove(s)
		if chat is None:
			continue
		err = e.send_text(chat, s["text"])
		speak(_("Scheduled message sent to %s") % e.chat_title(chat) if not err else _("A scheduled message could not be sent: %s") % err)
		changed(e)
	for r in [r for r in x["rem"] if r["ts"] <= t]:
		x["rem"].remove(r)
		sounds.play("notify", force=True)
		speak(r["text"])
		changed(e)


# ---------------------------------------------------------------- incoming messages: auto reply, quiet hours, translation, keywords
def in_quiet(e):
	q = X(e)["quiet"]
	if not q["on"]:
		return False
	h = time.localtime().tm_hour
	a, b = int(q["from"]), int(q["to"])
	return (a <= h < b) if a < b else (h >= a or h < b)


AUTO_MARK = "[Auto-reply] "


def on_incoming(ctrl, chat, msg, live):
	"""Returns True when the normal notification must be skipped."""
	e = ctrl.engine
	try:
		x = X(e)
		text = msg.get("x") or ""
		if live and chat["type"] == "dm" and not chat.get("request") and x["auto"]["on"] and msg["kind"] == "text" and not text.startswith(AUTO_MARK):
			last = x["auto"]["log"].get(chat["id"], 0)
			if time.time() - last > 12 * 3600:
				x["auto"]["log"][chat["id"]] = time.time()
				e.send_text(chat, AUTO_MARK + x["auto"]["text"])
				changed(e)
		hit = [k for k in x["keywords"] if k and k.lower() in text.lower()]
		if live and hit:
			sounds.play("notify", force=True)
			speak(_("Keyword alert: %s, from %s") % (hit[0], e.display_name(msg.get("f", ""))))
		if live and x["autotr"] and msg["kind"] == "text" and text and ai_config(e)["key"]:
			lang = target_lang(e)

			def work():
				try:
					r = ask_ai(e, "If this message is already written in %s, reply exactly SAME. Otherwise translate it into %s and reply with only the translation.\n\n%s"
						% (lang, lang, text))
				except Exception:
					return
				if r and r.strip().upper() != "SAME":
					wx.CallAfter(speak, _("Translation: %s") % r)
			threading.Thread(target=work, daemon=True).start()
		if in_quiet(e):
			return True
	except Exception:
		log.error("Friends Mirror: extras on_incoming failed", exc_info=True)
	return False


# ---------------------------------------------------------------- settings
def ai_settings(ctrl):
	e = ctrl.engine
	x = X(e)
	a = x["ai"]
	r = ask_form(_("AI settings"), [
		{"key": "p", "label": _("AI &provider:"), "type": "combo", "choices": [p[0] for p in PROVIDERS], "default": PROVIDERS[a.get("provider", 0)][0]},
		{"key": "key", "label": _("&API key:"), "default": a.get("key", "")},
		{"key": "model", "label": _("&Model (leave empty for the default):"), "default": a.get("model", "")},
		{"key": "base", "label": _("&Base URL (only for a custom provider):"), "default": a.get("base", "")},
		{"key": "lang", "label": _("My &language for translations and picture descriptions:"), "type": "combo", "choices": LANGS, "default": x["lang"]},
		{"key": "autotr", "label": _("Translate incoming messages &automatically and speak the translation"), "type": "check", "default": x["autotr"]},
	], intro=_("Google Gemini has a free plan. Pictures and voice messages need Google Gemini."))
	if not r:
		return
	x["ai"] = {"provider": r["p_index"], "key": r["key"], "model": r["model"], "base": r["base"]}
	x["lang"], x["autotr"] = r["lang"], r["autotr"]
	changed(e)
	speak(_("Saved"))


def quiet_settings(ctrl):
	e = ctrl.engine
	q = X(e)["quiet"]
	hours = ["%02d:00" % h for h in range(24)]
	r = ask_form(_("Quiet hours"), [
		{"key": "on", "label": _("Turn &quiet hours on (no sounds or speech for new messages)"), "type": "check", "default": q["on"]},
		{"key": "a", "label": _("&From:"), "type": "combo", "choices": hours, "default": hours[int(q["from"])]},
		{"key": "b", "label": _("&Until:"), "type": "combo", "choices": hours, "default": hours[int(q["to"])]},
	])
	if r:
		q.update({"on": r["on"], "from": r["a_index"], "to": r["b_index"]})
		changed(e)
		speak(_("Saved"))


def auto_reply_settings(ctrl):
	e = ctrl.engine
	a = X(e)["auto"]
	r = ask_form(_("Auto reply"), [
		{"key": "on", "label": _("Turn &auto reply on"), "type": "check", "default": a["on"]},
		{"key": "text", "label": _("&Message to send:"), "type": "multiline", "default": a["text"]},
	], intro=_("Each friend gets this reply once every 12 hours when they write to you. Groups are not answered."))
	if r:
		a["on"], a["text"] = r["on"], r["text"] or a["text"]
		changed(e)
		speak(_("Saved"))


def keyword_settings(ctrl):
	e = ctrl.engine
	x = X(e)
	r = ask_form(_("Keyword alerts"), [{"key": "k", "label": _("&Words to be alerted about, separated by commas:"), "default": ", ".join(x["keywords"])}],
		intro=_("When a new message contains one of these words you hear an alert, even if the chat is muted."))
	if r:
		x["keywords"] = [k.strip() for k in r["k"].split(",") if k.strip()]
		changed(e)
		speak(_("Saved"))


def _hash(pw, salt):
	return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), 60000).hex()


def lock_settings(ctrl):
	e = ctrl.engine
	x = X(e)
	has = bool(x.get("lock"))
	i = choose(_("&App lock:"), _("App lock"), [_("Set or change the lock password"), _("Remove the lock")] if has else [_("Set a lock password")])
	if i < 0:
		return
	if has and not check_lock(ctrl):
		return
	if has and i == 1:
		x.pop("lock", None)
		changed(e)
		speak(_("The lock was removed"))
		return
	r = ask_form(_("App lock"), [{"key": "p1", "label": _("&New lock password:"), "required": True}, {"key": "p2", "label": _("&Type it again:"), "required": True}],
		validate=lambda v: None if v["p1"] == v["p2"] and len(v["p1"]) >= 4 else _("The passwords must match and have at least 4 characters."))
	if r:
		salt = os.urandom(8).hex()
		x["lock"] = {"salt": salt, "h": _hash(r["p1"], salt)}
		changed(e)
		speak(_("Lock set. Friends Mirror will ask for it each time it is opened."))


def check_lock(ctrl):
	e = ctrl.engine
	lk = X(e).get("lock")
	if not lk:
		return True
	for _try in range(3):
		pw = ask_text(_("Lock password:"), _("Friends Mirror is locked"))
		if pw is None:
			return False
		if _hash(pw, lk["salt"]) == lk["h"]:
			return True
		speak(_("Wrong password"))
	return False


# ---------------------------------------------------------------- quick replies
def quick_replies(ctrl, window):
	e = ctrl.engine
	q = X(e)["quick"]
	if not q:
		speak(_("You have no quick replies. Add some in More tools, Quick replies."))
		return
	i = choose(_("&Quick reply:"), _("Quick replies"), q)
	if i >= 0:
		cur = window.input.GetValue()
		window.input.SetValue((cur + " " if cur else "") + q[i])
		window.input.SetInsertionPointEnd()
		window.input.SetFocus()


def manage_quick(ctrl):
	e = ctrl.engine
	q = X(e)["quick"]
	while True:
		i = choose(_("&Quick replies:"), _("Quick replies"), [_("Add a quick reply")] + q + ([_("Reset to the standard list")] if q != DEFAULT_QUICK else []))
		if i < 0:
			return
		if i == 0:
			t = ask_text(_("Quick reply text:"), _("Add a quick reply"))
			if t:
				q.append(t)
		elif i <= len(q):
			j = choose(_("&%s") % q[i - 1][:40], _("Quick reply"), [_("Edit"), _("Delete")])
			if j == 0:
				t = ask_text(_("Quick reply text:"), _("Edit"), q[i - 1])
				if t:
					q[i - 1] = t
			elif j == 1:
				del q[i - 1]
		else:
			q[:] = list(DEFAULT_QUICK)
		changed(e)


# ---------------------------------------------------------------- backup, export, unread
def backup(ctrl):
	import zipfile
	e = ctrl.engine
	path = None
	dlg = wx.FileDialog(gui.mainFrame, _("Save the backup as"), defaultFile=time.strftime("Friends Mirror backup %Y-%m-%d.zip"),
		wildcard="Zip (*.zip)|*.zip", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
	path = _modal(dlg, lambda d, c: d.GetPath() if c == wx.ID_OK else None)
	if not path:
		return
	try:
		e.save(True)
	except Exception:
		pass
	with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
		for n in os.listdir(e.dir):
			p = os.path.join(e.dir, n)
			if os.path.isfile(p) and n.startswith("account_"):
				z.write(p, n)
	speak(_("Backup saved. It contains your chats and your encryption keys. Keep it private."))


def restore(ctrl):
	import zipfile
	e = ctrl.engine
	dlg = wx.FileDialog(gui.mainFrame, _("Choose the backup file"), wildcard="Zip (*.zip)|*.zip", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
	path = _modal(dlg, lambda d, c: d.GetPath() if c == wx.ID_OK else None)
	if not path:
		return
	if not confirm(_("Restoring replaces the chats and keys of the same accounts on this computer. Continue?"), _("Restore backup")):
		return
	with zipfile.ZipFile(path) as z:
		for n in z.namelist():
			if n.startswith("account_") and n.endswith(".json") and "/" not in n and "\\" not in n:
				with open(os.path.join(e.dir, n), "wb") as f:
					f.write(z.read(n))
	speak(_("Backup restored. Restart NVDA, then sign in again."))


def export_all(ctrl):
	e = ctrl.engine
	dlg = wx.DirDialog(gui.mainFrame, _("Choose a folder for the exported chats"), style=wx.DD_DEFAULT_STYLE)
	folder = _modal(dlg, lambda d, c: d.GetPath() if c == wx.ID_OK else None)
	if not folder:
		return
	n = 0
	for c in e.d["chats"].values():
		if not c["msgs"]:
			continue
		name = re.sub(r"[^\w\- ]", "_", e.chat_title(c))[:40] or "chat"
		with open(os.path.join(folder, "%s_%s.txt" % (name, c["id"][-4:])), "w", encoding="utf-8") as f:
			f.write(e.export_chat(c))
		n += 1
	speak(_("%d chats exported") % n)


def read_unread(ctrl):
	e = ctrl.engine
	parts = []
	for c in e.chat_list("unread"):
		t = transcript(e, c, 20, unread=True)
		if t:
			parts.append(_("%s:\n%s") % (e.chat_title(c), t))
	if not parts:
		speak(_("No unread messages"))
		return
	show_text(_("Unread messages"), "\n\n".join(parts))


def catch_up_all(ctrl):
	e = ctrl.engine
	parts = []
	for c in e.chat_list("unread"):
		t = transcript(e, c, 30, unread=True)
		if t:
			parts.append(_("Chat %s:\n%s") % (e.chat_title(c), t))
	if not parts:
		speak(_("No unread messages"))
		return
	run_ai(ctrl, _("Catch up on all unread chats"), "Summarize each chat below in one or two sentences and say which ones need a reply.\n\n" + "\n\n".join(parts)[:20000])


# ---------------------------------------------------------------- AI assistant window
class AssistantDialog(wx.Dialog):
	def __init__(self, ctrl):
		super().__init__(gui.mainFrame, title=_("AI assistant"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.ctrl = ctrl
		self.hist = []
		s = wx.BoxSizer(wx.VERTICAL)
		h = guiHelper.BoxSizerHelper(self, sizer=s)
		self.view = h.addLabeledControl(_("Conversation:"), wx.TextCtrl, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(620, 300))
		self.input = h.addLabeledControl(_("Your &message (Enter to send):"), wx.TextCtrl, style=wx.TE_PROCESS_ENTER)
		bh = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.sendBtn = bh.addButton(self, label=_("&Send"))
		clr = bh.addButton(self, label=_("Clea&r"))
		cl = bh.addButton(self, id=wx.ID_CLOSE, label=_("C&lose"))
		h.addItem(bh)
		self.sendBtn.Bind(wx.EVT_BUTTON, self.onSend)
		self.input.Bind(wx.EVT_TEXT_ENTER, self.onSend)
		clr.Bind(wx.EVT_BUTTON, lambda ev: (self.hist.clear(), self.view.SetValue(""), speak(_("Cleared"))))
		cl.Bind(wx.EVT_BUTTON, lambda ev: self.EndModal(wx.ID_CLOSE))
		self.SetEscapeId(wx.ID_CLOSE)
		_finish(self, s)
		self.input.SetFocus()

	def onSend(self, ev):
		text = self.input.GetValue().strip()
		if not text or not self.sendBtn.IsEnabled():
			return
		self.input.SetValue("")
		self.view.AppendText(_("You: %s") % text + "\n\n")
		self.sendBtn.Disable()
		speak(_("Thinking"))
		hist = list(self.hist)
		e = self.ctrl.engine

		def work():
			try:
				r, err = ask_ai(e, text, history=hist), None
			except Exception as ex:
				r, err = None, str(ex)
			wx.CallAfter(done, r, err)

		def done(r, err):
			try:
				if err:
					self.view.AppendText(_("Error: %s") % err + "\n\n")
					speak(err)
				else:
					self.hist += [{"role": "user", "content": text}, {"role": "assistant", "content": r}]
					self.view.AppendText(_("AI: %s") % r + "\n\n")
					speak(r)
				self.sendBtn.Enable()
			except RuntimeError:
				pass
		threading.Thread(target=work, daemon=True).start()


def ai_assistant(ctrl):
	_modal(AssistantDialog(ctrl))


# ---------------------------------------------------------------- menus
def chat_tools(ctrl, chat, window):
	from .ui import run_menu
	opts = [
		(_("Suggest replies (AI)"), lambda: smart_replies(ctrl, chat, window)),
		(_("Rewrite my message (AI): polite, shorter, translate..."), lambda: rewrite(ctrl, chat, window)),
		(_("Write a message from an idea (AI)"), lambda: draft(ctrl, chat, window)),
		(_("Summarize this chat (AI)"), lambda: summarize(ctrl, chat, window)),
		(_("Catch up on unread messages (AI)"), lambda: catch_up(ctrl, chat, window)),
		(_("Ask AI a question about this chat"), lambda: ask_about_chat(ctrl, chat, window)),
		(_("Insert a quick reply"), lambda: quick_replies(ctrl, window)),
		(_("Send my message later (schedule)"), lambda: schedule_message(ctrl, chat, window)),
		(_("Read the last messages aloud"), lambda: read_last(ctrl, chat, window)),
		(_("Chat statistics"), lambda: stats(ctrl, chat, window)),
	]
	run_menu(_("Chat tools"), opts)


def message_options(ctrl, chat, window, m):
	"""Extra entries for the message actions menu."""
	if m.get("deleted"):
		return []
	opts = []
	if m["kind"] in ("text", "poll", "contact"):
		opts += [(_("Translate this message (AI)"), lambda: translate_message(ctrl, chat, window, m)),
			(_("Explain this message (AI)"), lambda: explain_message(ctrl, chat, window, m)),
			(_("Read this message aloud"), lambda: read_aloud(ctrl, chat, window, m))]
	if not m.get("own"):
		opts.append((_("Suggest replies to this message (AI)"), lambda: smart_replies(ctrl, chat, window, m)))
	if m["kind"] == "text" and links_of(ctrl.engine, chat, m):
		opts += [(_("Open the link in the browser"), lambda: open_link(ctrl, chat, window, m)),
			(_("Summarize the link (AI)"), lambda: summarize_link(ctrl, chat, window, m))]
	if m["kind"] == "file" and mimetypes.guess_type(m.get("at", {}).get("n", ""))[0] in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"):
		opts.append((_("Describe this picture (AI)"), lambda: describe_picture(ctrl, chat, window, m)))
	if m["kind"] == "voice":
		opts.append((_("Write down this voice message (AI)"), lambda: transcribe_voice(ctrl, chat, window, m)))
	opts.append((_("Remind me about this message"), lambda: remind_me(ctrl, chat, window, m)))
	return opts


def main_tools(ctrl):
	from .ui import run_menu
	run_menu(_("More tools"), [
		(_("AI assistant: ask anything"), lambda: ai_assistant(ctrl)),
		(_("Catch up on all unread chats (AI)"), lambda: catch_up_all(ctrl)),
		(_("Read all unread messages"), lambda: read_unread(ctrl)),
		(_("New reminder"), lambda: new_reminder(ctrl)),
		(_("Scheduled messages"), lambda: manage_scheduled(ctrl)),
		(_("Quick replies"), lambda: manage_quick(ctrl)),
		(_("Export all chats to text files"), lambda: export_all(ctrl)),
		(_("Back up chats and keys"), lambda: backup(ctrl)),
		(_("Restore a backup"), lambda: restore(ctrl)),
	])


def settings_items(ctrl):
	return [
		(_("AI settings: provider, key and language"), lambda: ai_settings(ctrl)),
		(_("Quiet hours"), lambda: quiet_settings(ctrl)),
		(_("Auto reply"), lambda: auto_reply_settings(ctrl)),
		(_("Keyword alerts"), lambda: keyword_settings(ctrl)),
		(_("App lock with a password"), lambda: lock_settings(ctrl)),
	]


# ---------------------------------------------------------------- timer
_timer = [None]


def start(ctrl):
	stop()
	t = wx.PyTimer(lambda: tick(ctrl)) if hasattr(wx, "PyTimer") else wx.Timer()
	if not hasattr(wx, "PyTimer"):
		t.Notify = lambda: tick(ctrl)
	t.Start(5000)
	_timer[0] = t


def stop():
	t = _timer[0]
	_timer[0] = None
	if t:
		try:
			t.Stop()
		except Exception:
			pass
