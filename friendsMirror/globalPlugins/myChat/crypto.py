# -*- coding: UTF-8 -*-
"""Cryptography for myChat, using only the Python standard library.

* X25519 key agreement (RFC 7748), written in plain Python.
* Account keys come from the username and password with scrypt, so the same name and password give the
  same keys on every computer. The password never leaves the computer.
* Messages are encrypted with a SHAKE-256 keystream and authenticated with HMAC-SHA256 (encrypt-then-MAC).
* Only the two chat partners (or the members of a group) can read a message. The server sees random bytes.
"""
import base64
import hashlib
import hmac
import os

P = 2 ** 255 - 19
A24 = 121665
BASE = (9).to_bytes(32, "little")


def x25519(k, u):
	"""Scalar multiplication on Curve25519 (RFC 7748). k and u are 32 bytes."""
	k = bytearray(k)
	k[0] &= 248
	k[31] &= 127
	k[31] |= 64
	kk = int.from_bytes(k, "little")
	x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
	x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
	for t in range(254, -1, -1):
		kt = (kk >> t) & 1
		swap ^= kt
		if swap:
			x2, x3 = x3, x2
			z2, z3 = z3, z2
		swap = kt
		a = (x2 + z2) % P
		aa = a * a % P
		b = (x2 - z2) % P
		bb = b * b % P
		e = (aa - bb) % P
		c = (x3 + z3) % P
		d = (x3 - z3) % P
		da = d * a % P
		cb = c * b % P
		x3 = (da + cb) ** 2 % P
		z3 = x1 * (da - cb) ** 2 % P
		x2 = aa * bb % P
		z2 = e * (aa + A24 * e) % P
	if swap:
		x2, x3 = x3, x2
		z2, z3 = z3, z2
	return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


def public_key(priv):
	return x25519(priv, BASE)


def b64(data):
	return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text):
	text = str(text)
	return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def account_keys(username, password):
	"""Returns (private key, public key) for a user name and password. The same input always gives the same keys."""
	salt = b"myChat|account|v1|" + username.strip().lower().encode("utf-8")
	pw = password.encode("utf-8")
	try:
		seed = hashlib.scrypt(pw, salt=salt, n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
	except (AttributeError, ValueError):
		seed = hashlib.pbkdf2_hmac("sha256", pw, salt, 300000, 32)
	return seed, public_key(seed)


def hkdf(ikm, salt, info, length):
	prk = hmac.new(salt, ikm, hashlib.sha256).digest()
	out, block, i = b"", b"", 1
	while len(out) < length:
		block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
		out += block
		i += 1
	return out[:length]


_pair_cache = {}


def pair_key(my_priv, my_pub, their_pub):
	"""64-byte key shared by two users. Both sides compute the same value."""
	ck = (my_priv, their_pub)
	if ck not in _pair_cache:
		dh = x25519(my_priv, their_pub)
		if dh == bytes(32):
			raise ValueError("bad public key")
		a, b = sorted([my_pub, their_pub])
		_pair_cache[ck] = hkdf(dh, a + b, b"myChat pair key v1", 64)
	return _pair_cache[ck]


def new_key():
	return os.urandom(64)


def seal(key, data, aad=b""):
	"""Encrypt and authenticate. key is 64 bytes. Returns nonce + ciphertext + tag."""
	nonce = os.urandom(16)
	ks = hashlib.shake_256(key[:32] + nonce).digest(len(data))
	ct = (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big")
	tag = hmac.digest(key[32:], aad + nonce + ct, "sha256")
	return nonce + ct + tag


def open_sealed(key, blob, aad=b""):
	"""Check and decrypt. Raises ValueError when the data was changed or the key is wrong."""
	if len(blob) < 48:
		raise ValueError("too short")
	nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
	good = hmac.digest(key[32:], aad + nonce + ct, "sha256")
	if not hmac.compare_digest(good, tag):
		raise ValueError("authentication failed")
	ks = hashlib.shake_256(key[:32] + nonce).digest(len(ct))
	return (int.from_bytes(ct, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(ct), "big")


def _hex(*parts):
	return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def inbox_topic(username, pub_b64):
	return "mci" + _hex("inbox", username.lower(), pub_b64)[:30]


def directory_topic(username):
	return "mcd" + _hex("directory", username.lower())[:30]


def presence_topic(secret):
	return "mcp" + _hex("presence", secret)[:30]


def group_topic(group_id):
	return "mcg" + _hex("group", group_id)[:30]


def file_topic():
	return "mcf" + os.urandom(12).hex()


def safety_number(pub_a, pub_b):
	"""Sixty digits in groups of five. Both chat partners see the same number; compare it by voice to be sure."""
	a, b = sorted([pub_a, pub_b])
	n = int.from_bytes(hashlib.sha256(b"myChat safety" + a + b).digest(), "big")
	digits = str(n).zfill(77)[:60]
	return " ".join(digits[i:i + 5] for i in range(0, 60, 5))


def fingerprint(pub):
	return hashlib.sha256(pub).hexdigest()[:16]
