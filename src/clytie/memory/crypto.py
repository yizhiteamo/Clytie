"""标准库算法包装：独立口令与恢复码两条解密路径。"""

import base64
import hashlib
import json
import os
import unicodedata

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .store import canonical


class PackageError(ValueError):
    pass


def b64(value):
    return base64.b64encode(value).decode("ascii")


def un64(value):
    return base64.b64decode(value, validate=True)


def public_bytes(key):
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def public_key(data):
    key = serialization.load_der_public_key(data)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("invalid recovery public key")
    return key


def new_recovery():
    key = ec.generate_private_key(ec.SECP256R1())
    code = "clytie-recovery-v1:" + b64(key.private_numbers().private_value.to_bytes(32, "big"))
    return code, public_bytes(key.public_key())


def password_key(passphrase, salt):
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        unicodedata.normalize("NFC", passphrase).encode("utf-8")
    )


def exchange(key, public, salt, recipient, ephemeral):
    shared = key.exchange(ec.ECDH(), public)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"clytie-memory-recovery-v1\0" + recipient + ephemeral,
    ).derive(shared)


def encrypt(payload, passphrase, recipient):
    if len(passphrase) < 12:
        raise ValueError("passphrase must contain at least 12 characters")
    recipient_key = public_key(recipient)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    ephemeral_bytes = public_bytes(ephemeral.public_key())
    salt, recovery_salt = os.urandom(16), os.urandom(16)
    base = {
        "format": "clytie-memory",
        "version": 1,
        "cipher": "AES-256-GCM",
        "kdf": {"name": "scrypt", "n": 2**15, "r": 8, "p": 1, "salt": b64(salt)},
        "recovery": {
            "curve": "P-256",
            "salt": b64(recovery_salt),
            "recipient": b64(recipient),
            "ephemeral": b64(ephemeral_bytes),
            "fingerprint": hashlib.sha256(recipient).hexdigest(),
        },
    }
    content_key = AESGCM.generate_key(bit_length=256)
    wrapped = {}
    keys = {
        "passphrase": password_key(passphrase, salt),
        "recovery": exchange(ephemeral, recipient_key, recovery_salt, recipient, ephemeral_bytes),
    }
    for name, key in keys.items():
        nonce = os.urandom(12)
        aad = canonical({"base": base, "wrapper": name}).encode()
        wrapped[name] = {
            "nonce": b64(nonce),
            "data": b64(AESGCM(key).encrypt(nonce, content_key, aad)),
        }
    header = canonical({"base": base, "wrapped": wrapped}).encode()
    nonce = os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(nonce, payload, header)
    return canonical({"header": b64(header), "nonce": b64(nonce), "data": b64(ciphertext)}).encode()


def decrypt(package, *, passphrase=None, recovery_code=None):
    try:
        if (passphrase is None) == (recovery_code is None):
            raise ValueError("provide exactly one private_text")
        outer = json.loads(package)
        header = un64(outer["header"])
        details = json.loads(header)
        base = details["base"]
        if (
            base["format"] != "clytie-memory"
            or base["version"] != 1
            or base["cipher"] != "AES-256-GCM"
        ):
            raise ValueError("unsupported format")
        kdf, recovery = base["kdf"], base["recovery"]
        if (kdf["name"], kdf["n"], kdf["r"], kdf["p"]) != ("scrypt", 2**15, 8, 1):
            raise ValueError("unsupported KDF")
        recipient, ephemeral = un64(recovery["recipient"]), un64(recovery["ephemeral"])
        public_key(recipient)
        eph = public_key(ephemeral)
        if (
            recovery["curve"] != "P-256"
            or recovery["fingerprint"] != hashlib.sha256(recipient).hexdigest()
        ):
            raise ValueError("invalid recovery metadata")
        if passphrase is not None:
            name, key = "passphrase", password_key(passphrase, un64(kdf["salt"]))
        else:
            prefix, encoded = recovery_code.rsplit(":", 1)
            if prefix != "clytie-recovery-v1":
                raise ValueError("invalid recovery version")
            raw = un64(encoded)
            if len(raw) != 32:
                raise ValueError("invalid recovery code")
            private = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
            if public_bytes(private.public_key()) != recipient:
                raise ValueError("wrong recovery code")
            name = "recovery"
            key = exchange(private, eph, un64(recovery["salt"]), recipient, ephemeral)
        wrapper = details["wrapped"][name]
        content_key = AESGCM(key).decrypt(
            un64(wrapper["nonce"]),
            un64(wrapper["data"]),
            canonical({"base": base, "wrapper": name}).encode(),
        )
        return AESGCM(content_key).decrypt(un64(outer["nonce"]), un64(outer["data"]), header)
    except Exception as exc:
        raise PackageError("口令或恢复码错误，或记忆包已损坏") from exc
