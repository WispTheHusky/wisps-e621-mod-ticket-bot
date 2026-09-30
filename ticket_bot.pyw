"""Wisp's e621 Mod Ticket Bot: one Windows application source.

Requires Python 3.10+ with Tk and Pillow for the application graphics.
All application data stays beside this file (or the packaged executable).
Saved keys use Windows account encryption. No ticket-changing requests exist.
"""
import base64
import json
import os
import math
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import io
import re
import ctypes as C
from ctypes import wintypes as W
import subprocess
import time
import winsound
from contextlib import contextmanager
from functools import wraps
from functools import lru_cache
from array import array
import webbrowser
import tkinter as tk
from tkinter import font as tkfont
import argparse
import queue
import random
import hashlib
import signal
import sys
sys.dont_write_bytecode = True
import threading
import wave
from tkinter import messagebox
import zlib
import winreg
import importlib


def load_graphics():
    """Load one consistent graphics stack, including Tk support, before startup."""
    global Image, ImageDraw, ImageChops, ImageOps, ImageTk
    try:
        importlib.invalidate_caches()
        from PIL import Image, ImageDraw, ImageChops, ImageOps, ImageTk
        Image.Resampling.LANCZOS
        Image.Transpose.TRANSPOSE
    except (ImportError, OSError, AttributeError):
        Image = ImageDraw = ImageChops = ImageOps = ImageTk = None
        return False
    return True


load_graphics()


class GraphicsUnavailable(RuntimeError):
    pass


# Read-only queue client and durable history

ENDPOINT = 'https://e621.net/tickets.json'

class MonitorError(Exception):

    def __init__(self, message, retry_after=0, retryable=True):
        super().__init__(message)
        self.retry_after = retry_after
        self.retryable = retryable

def http_failure(exc):
    """Map allowlisted server messages to safe advice; never echo raw errors."""
    delay = retry_seconds(exc.headers.get('Retry-After'))
    detail = None
    if exc.code in (401, 403):
        try:
            payload = json.loads(exc.read(65536))
            if isinstance(payload, dict) and isinstance(payload.get('message'), str):
                detail = payload['message']
        except (ValueError, OSError, TypeError, AttributeError):
            pass
    if exc.code == 401:
        if detail == 'SessionLoader::AuthenticationFailure':
            message = 'HTTP 401: e621 rejected the configured username/API-key pair. Verify the username and enter an active key using Login.'
        elif detail == 'Insufficient scope':
            message = 'HTTP 401: server reports insufficient authentication scope. Check key access, then restart.'
        elif detail == 'Account level below application minimum':
            message = 'HTTP 401: server reports an account-level restriction. Resolve account access, then restart.'
        else:
            message = 'HTTP 401: authentication was not accepted; the response did not identify an allowlisted cause. Verify local credentials and site access, then restart.'
        return MonitorError(message, delay, retryable=False)
    if exc.code == 403:
        message = 'HTTP 403: server requires browser authentication for this request.' if detail == 'This action requires browser authentication' else 'HTTP 403: access denied; the response does not establish whether credentials, permissions, or a site filter caused it.'
        return MonitorError(message + ' Resolve access before restarting.', delay, retryable=False)
    return MonitorError(f'HTTP {exc.code}: request failed; state unchanged.', delay)

def retry_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, *args, **kwargs):
        return None

def validate_request(request):
    """Fail closed before every network call. No configurable routes or methods."""
    url = urllib.parse.urlsplit(request.full_url)
    pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
    params = dict(pairs)
    if request.get_method() != 'GET' or request.data is not None or url.scheme != 'https' or (url.netloc != 'e621.net') or (url.path != '/tickets.json') or url.fragment or (len(pairs) != len(params)) or set(params) - {'search[status]', 'limit', 'page'} or (params.get('search[status]') != 'pending_unclaimed') or (not params.get('limit', '').isdigit()) or (not 1 <= int(params['limit']) <= 100) or ('page' in params and (not params['page'].startswith('b') or not params['page'][1:].isdigit())):
        raise MonitorError('Unsafe network request blocked locally.')

def parse_page(payload):
    if not isinstance(payload, list):
        raise MonitorError('Unexpected API response; expected a ticket array. State unchanged.')
    ids = []
    for row in payload:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
            raise MonitorError('Invalid ticket data. State unchanged.')
        if row.get('status') != 'pending' or row.get('claimant_id') is not None:
            raise MonitorError('API returned a ticket outside the requested queue. State unchanged.')
        ids.append(row['id'])
    if ids != sorted(set(ids), reverse=True):
        raise MonitorError('Unexpected ticket order. State unchanged.')
    return {row['id']: row.get('reason') if isinstance(row.get('reason'), str) else None for row in payload}

class Client:

    def __init__(self, config, key, stop, opener=None):
        if not key or key.strip().upper() == 'KEYPLACEHOLDER':
            raise MonitorError('A real API key is required. Use Login to enter it.')
        self.config, self.stop = (config, stop)
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        token = base64.b64encode(f"{config['username']}:{key}".encode()).decode()
        self.headers = {'Authorization': 'Basic ' + token, 'Accept': 'application/json', 'User-Agent': f"E621TicketBot/1.0 (by {config['username']} on e621)"}

    def page(self, cursor):
        params = {'search[status]': 'pending_unclaimed', 'limit': self.config['page_size']}
        if cursor is not None:
            params['page'] = f'b{cursor}'
        request = urllib.request.Request(ENDPOINT + '?' + urllib.parse.urlencode(params), headers=self.headers, method='GET')
        validate_request(request)
        try:
            with self.opener.open(request, timeout=self.config['request_timeout_seconds']) as response:
                if response.status != 200:
                    raise MonitorError('Unexpected HTTP response. State unchanged.')
                raw = response.read(8000001)
                if len(raw) > 8000000:
                    raise MonitorError('API response too large. State unchanged.')
                return parse_page(json.loads(raw))
        except urllib.error.HTTPError as exc:
            raise http_failure(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise MonitorError('Network connection failed or timed out; state unchanged.') from None
        except (ValueError, UnicodeError):
            raise MonitorError('Invalid JSON response; state unchanged.') from None

    def snapshot(self):
        found, cursor = ({}, None)
        for _ in range(self.config['max_pages']):
            if self.stop.is_set():
                raise MonitorError('Stopped before queue scan completed; state unchanged.')
            ids = self.page(cursor)
            if not ids:
                return found
            if cursor is not None and max(ids) >= cursor:
                raise MonitorError('Pagination did not advance; state unchanged.')
            found.update(ids)
            cursor = min(ids)
            if self.stop.wait(1):
                raise MonitorError('Stopped before queue scan completed; state unchanged.')
        raise MonitorError('Queue exceeds max_pages; no partial baseline was saved.')

class State:

    def __init__(self, path, username):
        self.path, self.username = (Path(path), username)
        self.initialized, self.seen, self.pending = (False, set(), 0)
        self.last_ticket_found = None
        self.history = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data['version'] != 1 or data['username'] != username:
                    raise ValueError()
                if type(data['initialized']) is not bool or type(data['pending_visuals']) is not int or data['pending_visuals'] < 0:
                    raise ValueError()
                if not isinstance(data['seen_ids'], list) or any((type(i) is not int or i <= 0 for i in data['seen_ids'])):
                    raise ValueError()
                self.initialized = data['initialized']
                self.seen = set(data['seen_ids'])
                self.pending = data['pending_visuals']
                saved_time = data.get('last_ticket_found')
                if saved_time is not None:
                    parsed = datetime.fromisoformat(saved_time)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError()
                    self.last_ticket_found = parsed.astimezone(timezone.utc)
                history = data.get('recent_tickets', [])
                if not isinstance(history, list) or len(history) > 10:
                    raise ValueError()
                for row in history:
                    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] not in self.seen:
                        raise ValueError()
                    reason = row.get('reason')
                    if reason is not None and (not isinstance(reason, str) or len(reason) > 10064):
                        raise ValueError()
                    when = datetime.fromisoformat(row['detected_at'])
                    if when.tzinfo is None:
                        raise ValueError()
                    self.history.append((row['id'], reason, when.astimezone(timezone.utc)))
            except (KeyError, ValueError, TypeError):
                raise MonitorError('State file is invalid or belongs to another username. Preserve it and consult README.') from None

    def save(self, seen, pending, initialized=True, last_ticket_found=None, history=None):
        timestamp = last_ticket_found if last_ticket_found is not None else self.last_ticket_found
        history = self.history if history is None else history
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        data = {'version': 1, 'username': self.username, 'initialized': initialized, 'seen_ids': sorted(seen), 'pending_visuals': pending, 'last_ticket_found': timestamp.isoformat() if timestamp else None, 'recent_tickets': [{'id': i, 'reason': r, 'detected_at': d.isoformat()} for i, r, d in history]}
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self.seen, self.pending, self.initialized = (seen, pending, initialized)
        self.last_ticket_found = timestamp
        self.history = history

    def accept(self, ids, snapshot=None):
        fresh = ids - self.seen if self.initialized else set()
        detected_at = datetime.now(timezone.utc) if fresh else None
        recent = RecentTickets(limit=10)
        recent.rows = list(self.history)
        recent.add(fresh, snapshot or {}, detected_at)
        self.save(self.seen | ids, self.pending + len(fresh), last_ticket_found=detected_at, history=recent.rows)
        return fresh

    def acknowledge_visuals(self, count):
        self.save(self.seen, max(0, self.pending - count), self.initialized)

class VisualGate:
    """Wait for two safe desktop checks; collect a single count while fullscreen."""

    def __init__(self):
        self.safe_checks = 0

    def ready(self, blocked, count):
        self.safe_checks = 0 if blocked else self.safe_checks + 1
        return count > 0 and self.safe_checks >= 2

class SoundGate:
    """Suppress burst replays; never enqueue a backlog of sounds."""

    def __init__(self):
        self.available_at = 0

    def ready(self, now, duration):
        if now < self.available_at:
            return False
        self.available_at = now + duration
        return True


class RecentTickets:
    """Bounded moderator details; only real detections may enter persisted state."""

    def __init__(self, limit=10):
        self.limit, self.rows = (limit, [])

    def add(self, ids, snapshot, detected_at):
        for ticket_id in sorted(ids):
            reason = snapshot.get(ticket_id) if isinstance(snapshot, dict) else None
            if isinstance(reason, str):
                reason = ''.join((c for c in reason if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127)))
                if len(reason) > 10000:
                    reason = reason[:10000] + '\n[Display truncated at 10,000 characters]'
            else:
                reason = None
            self.rows.insert(0, (ticket_id, reason, detected_at))
        del self.rows[self.limit:]

# Polling schedule

def queue_message(count):
    return f"Queue currently has {count} {('ticket' if count == 1 else 'tickets')}."

class PollSchedule:

    def __init__(self, interval=5, enabled=True):
        self.interval = interval
        self.enabled = enabled
        self.paused = False
        self.testing = False
        self.terminal = False
        self.inflight = False
        self.deadline = 0.0
        self.wait_started = 0.0
        self.completed_at = None
        self.not_before = 0.0
        self.backoff = False
        self.scan_started = None
        self.present_until = 0.0
        self.failures = 0
        self.paused_remaining = None
        self.paused_total = None

    def ready(self, now):
        return self.enabled and (not (self.paused or self.testing or self.terminal or self.inflight)) and (now >= self.deadline)

    def begin(self, now):
        if not self.ready(now):
            return False
        self.inflight = True
        self.scan_started = now
        self.present_until = now + 1
        return True

    def finish(self, now, retry_delay=None, terminal=False):
        self.inflight = False
        self.terminal = terminal
        self.completed_at = now
        self.wait_started = now
        self.backoff = retry_delay is not None
        self.not_before = now + retry_delay if self.backoff else 0
        self.deadline = self.not_before if self.backoff else now + self.interval
        if self.paused:
            self.paused_remaining = max(0, self.deadline - now)
            self.paused_total = self.paused_remaining
        if terminal:
            self.present_until = 0

    def toggle_pause(self, now):
        if self.testing or self.terminal or (not self.enabled):
            return
        self.paused = not self.paused
        if self.paused:
            self.paused_remaining = max(0, self.deadline - now)
            self.paused_total = max(0.001, self.deadline - max(self.wait_started, self.present_until))
        self.present_until = 0
        if not self.paused and (not self.inflight):
            self.deadline = max(now + (self.paused_remaining or 0), self.not_before)
            self.wait_started = self.deadline - (self.paused_total or self.interval)

    def set_interval(self, seconds, now):
        if type(seconds) is not int or not 5 <= seconds <= 3600:
            raise ValueError('Refresh interval must be a whole number from 5 to 3600 seconds.')
        self.interval = seconds
        if self.paused:
            self.paused_remaining = max(seconds, self.not_before - now)
            self.paused_total = self.paused_remaining
        if not self.inflight and (not self.backoff):
            self.wait_started = now
            self.deadline = now + seconds

    def start_test(self):
        if self.testing:
            return False
        self.testing = True
        self.present_until = 0
        return True

    def end_test(self, now):
        self.testing = False
        self.present_until = 0
        if not self.inflight:
            self.wait_started = now
            self.deadline = max(now + self.interval, self.not_before)

    def presentation(self, now):
        if self.testing:
            return ('Settling current request...' if self.inflight else 'Ticket Test · live refresh paused', 0, 'test')
        if self.terminal:
            return ('Polling stopped · resolve access before restarting', 0, 'stopped')
        if self.paused:
            fraction = min(1, (self.paused_remaining or 0) / max(0.001, self.paused_total or 1))
            return ('Paused · finishing current request' if self.inflight else f'Paused · {math.ceil(self.paused_remaining or 0)}s remaining', fraction, 'paused')
        if not self.enabled:
            return ('Stopped · press Start to monitor', 0, 'idle')
        if self.inflight or now < self.present_until:
            dots = '.' * (int(max(0, now - (self.scan_started or 0)) * 8) % 4)
            return ('Scanning queue' + dots, 0, 'scanning')
        remaining = max(0, self.deadline - now)
        visible_start = max(self.wait_started, self.present_until)
        total = max(0.001, self.deadline - visible_start)
        return (f"{('Retry' if self.backoff else 'Next refresh')} in {math.ceil(remaining)}s", min(1, remaining / total), 'backoff' if self.backoff else 'waiting')

# Public-only avatar lookup

MAX_BYTES = 2000000

class AvatarUnavailable(Exception):
    pass

def validate_avatar_request(request, allowed_url, media=False):
    u = urllib.parse.urlsplit(request.full_url)
    if request.full_url != allowed_url or request.get_method() != 'GET' or request.data is not None or (u.scheme != 'https') or u.fragment or u.query or any((k.lower() in ('authorization', 'cookie', 'proxy-authorization') for k, v in request.header_items())):
        raise AvatarUnavailable('Unsafe avatar request blocked.')
    if media:
        valid = u.netloc in ('static1.e621.net', 'e621.net') and re.fullmatch('/data/(?:preview/(?:[a-f0-9]{2}/[a-f0-9]{2}/)?[a-f0-9]{32}\\.(?:jpg|png|webp)|avatars/[1-9][0-9]*\\.jpg)', u.path)
    else:
        valid = u.netloc == 'e621.net' and (re.fullmatch('/users/[A-Za-z0-9_~-]{1,100}\\.json', u.path) or re.fullmatch('/posts/[1-9][0-9]*\\.json', u.path))
    if not valid:
        raise AvatarUnavailable('Unapproved avatar host or path.')

def read_public(url, username, opener, media=False):
    request = urllib.request.Request(url, headers={'User-Agent': f'E621TicketBot/1.0 (by {username} on e621)', 'Accept': 'image/jpeg,image/png,image/webp' if media else 'application/json'}, method='GET')
    validate_avatar_request(request, url, media)
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise AvatarUnavailable('Avatar response unavailable.')
        mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if media and mime not in ('image/jpeg', 'image/png', 'image/webp'):
            raise AvatarUnavailable('Unsupported avatar image type.')
        if not media and mime != 'application/json':
            raise AvatarUnavailable('Invalid profile response type.')
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise AvatarUnavailable('Avatar response too large.')
        return data

def decode_thumbnail(data):
    if Image is None:
        raise AvatarUnavailable('Avatar decoder unavailable; initials shown.') from None
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in ('JPEG', 'PNG', 'WEBP') or im.width * im.height > 4000000 or min(im.size) < 1:
            raise AvatarUnavailable('Unsupported avatar dimensions or format.')
        im.seek(0)
        im = ImageOps.fit(im.convert('RGBA'), (64, 64))
        mask = Image.new('L', (64, 64), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
        im.putalpha(mask)
        out = io.BytesIO()
        im.convert('RGBA').save(out, format='PNG')
        return out.getvalue()

def fetch_avatar(username, stop, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        if not re.fullmatch('[A-Za-z0-9_~-]{1,100}', username):
            raise AvatarUnavailable('Profile name is unsupported by the avatar lookup; initials shown.')
        path = urllib.parse.quote(username, safe='')
        profile = json.loads(read_public(f'https://e621.net/users/{path}.json', username, opener))
        if not isinstance(profile, dict) or not isinstance(profile.get('name'), str) or profile['name'].casefold() != username.casefold():
            raise AvatarUnavailable('Profile identity mismatch.')
        user_id, post_id = (profile.get('id'), profile.get('avatar_id'))
        if type(user_id) is not int or user_id <= 0 or type(post_id) is not int or (post_id <= 0):
            raise AvatarUnavailable('No profile avatar configured.')
        if stop.wait(1):
            raise AvatarUnavailable('Avatar lookup stopped.')
        post = json.loads(read_public(f'https://e621.net/posts/{post_id}.json', username, opener)).get('post')
        if not isinstance(post, dict) or type(post.get('id')) is not int or post.get('id') != post_id or post.get('flags', {}).get('deleted'):
            raise AvatarUnavailable('Avatar post unavailable.')
        url = post.get('preview', {}).get('url')
        if not isinstance(url, str):
            raise AvatarUnavailable('Public avatar preview unavailable.')
        validate_avatar_request(urllib.request.Request(url, method='GET'), url, True)
        if profile.get('has_cropped_avatar', profile.get('has_cropped_avatar?', False)) is True:
            origin = urllib.parse.urlsplit(url)
            url = f'https://{origin.netloc}/data/avatars/{user_id}.jpg'
        if stop.wait(1):
            raise AvatarUnavailable('Avatar lookup stopped.')
        return (decode_thumbnail(read_public(url, username, opener, media=True)), 'Profile avatar loaded')
    except AvatarUnavailable as exc:
        return (None, str(exc))
    except Exception:
        return (None, 'Avatar unavailable; initials shown.')

# Halo geometry and colors

HALO_BLUE = (1, 84, 157)
HALO_YELLOW = (252, 191, 49)
PALETTE = [tuple((round(a + (b - a) * i / 64) for a, b in zip(HALO_BLUE, HALO_YELLOW))) for i in range(65)]

def color_index(position, elapsed):
    wave = math.sin(4 * math.pi * (position - elapsed / 3))
    return round(64 * max(0, min(1, 0.5 + 2 * wave)))

def bands(width, height, thickness):
    """Return nonoverlapping ring strips with local paint rectangles and phase."""
    thickness = min(thickness, min(width, height) // 4)
    result = []
    for layer in range(10):
        inset = round(layer * thickness / 10)
        step = round((layer + 1) * thickness / 10) - inset
        if step <= 0:
            continue
        w, h = (width - 2 * inset, height - 2 * inset)
        perimeter = 2 * (w + h)
        edges = [(inset, inset, w, step, 0), (width - inset - step, inset + step, step, h - 2 * step, 1), (inset, height - inset - step, w, step, 2), (inset, inset + step, step, h - 2 * step, 3)]
        for x, y, bw, bh, edge in edges:
            horizontal = edge in (0, 2)
            length = bw if horizontal else bh
            count = max(1, round(length / perimeter * 96))
            paint = []
            for i in range(count):
                a, b = (round(i * length / count), round((i + 1) * length / count))
                mid = (a + b) / 2
                distance = mid if edge == 0 else w + step + mid if edge == 1 else w + h + w - mid if edge == 2 else perimeter - step - mid
                rect = (a, 0, b, bh) if horizontal else (0, a, bw, b)
                paint.append((rect, distance / perimeter))
            result.append(((x, y, bw, bh), (1 - layer / 10) ** 1.7 * 0.94, paint))
    return result

# Smooth edge textures

class EdgeTexture:

    def __init__(self, width, height, thickness, edge):
        t = min(thickness, min(width, height) // 4)
        self.horizontal = edge in (0, 2)
        self.rect = ((0, 0, width, t), (width - t, t, t, height - 2 * t), (0, height - t, width, t), (0, t, t, height - 2 * t))[edge]
        _, _, w, h = self.rect
        gradient = [round(240 * (1 - d / max(1, t - 1)) ** 1.7) for d in range(t)]
        alpha = Image.new('L', (w, h))
        draw = ImageDraw.Draw(alpha)
        for d, value in enumerate(gradient):
            if self.horizontal:
                y = d if edge == 0 else h - 1 - d
                draw.line((0, y, w - 1, y), fill=value)
            else:
                x = d if edge == 3 else w - 1 - d
                draw.line((x, 0, x, h - 1), fill=value)
        if self.horizontal:
            corners = Image.new('L', (w, h))
            draw = ImageDraw.Draw(corners)
            for d, value in enumerate(gradient):
                draw.line((d, 0, d, h - 1), fill=value)
                draw.line((w - 1 - d, 0, w - 1 - d, h - 1), fill=value)
            alpha = ImageChops.lighter(alpha, corners)
        self.alpha = alpha
        perimeter = 2 * (width + height)
        length = w if self.horizontal else h
        count = max(2, round(length / perimeter * 192))
        self.positions = []
        for i in range(count):
            p = i * length / max(1, count - 1)
            distance = p if edge == 0 else width + t + p if edge == 1 else width + height + width - p if edge == 2 else perimeter - t - p
            self.positions.append(distance / perimeter)

    def frame(self, elapsed):
        _, _, w, h = self.rect
        colors = Image.new('RGB', (len(self.positions), 1))
        colors.putdata([PALETTE[color_index(p, elapsed)] for p in self.positions])
        if not self.horizontal:
            colors = colors.transpose(Image.Transpose.TRANSPOSE)
        colors = colors.resize((w, h), Image.Resampling.BILINEAR)
        r, g, b = colors.split()
        return Image.merge('RGBA', (b, g, r, self.alpha)).convert('RGBa').tobytes()

# Windows integration

user = C.WinDLL('user32', use_last_error=True)
kernel = C.WinDLL('kernel32', use_last_error=True)
shell = C.WinDLL('shell32', use_last_error=True)

def set_app_identity():
    """Use the same identity for our taskbar group and native notifications."""
    result = bind(shell, 'SetCurrentProcessExplicitAppUserModelID', [W.LPCWSTR], C.c_long)('Wisp.E621TicketBot')
    if result < 0:
        raise OSError('Could not set Windows app identity.')

def bind(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = (args, result)
    return fn
try:
    set_thread_dpi = bind(user, 'SetThreadDpiAwarenessContext', [C.c_void_p], C.c_void_p)
except AttributeError:
    set_thread_dpi = None

def configure_dashboard_dpi():
    """Keep Tk sharp at fixed pixel dimensions, without PMv2 frame-size drift."""
    try:
        bind(user, 'SetProcessDpiAwarenessContext', [C.c_void_p], W.BOOL)(C.c_void_p(-4))
    except AttributeError:
        bind(user, 'SetProcessDPIAware', [], W.BOOL)()
    if set_thread_dpi:
        if not set_thread_dpi(C.c_void_p(-3)):
            raise C.WinError(C.get_last_error())

@contextmanager
def physical_pixels():
    """Scope native screen operations to physical pixels, restoring Tk's context."""
    previous = set_thread_dpi(C.c_void_p(-4)) if set_thread_dpi else None
    try:
        yield
    finally:
        if previous:
            set_thread_dpi(previous)

def native_pixels(function):

    @wraps(function)
    def call(*args, **kwargs):
        with physical_pixels():
            return function(*args, **kwargs)
    return call
foreground = bind(user, 'GetForegroundWindow', [], W.HWND)
get_rect = bind(user, 'GetWindowRect', [W.HWND, C.POINTER(W.RECT)], W.BOOL)
monitor_from = bind(user, 'MonitorFromWindow', [W.HWND, W.DWORD], W.HANDLE)
get_class = bind(user, 'GetClassNameW', [W.HWND, W.LPWSTR, C.c_int], C.c_int)
get_style = bind(user, 'GetWindowLongPtrW' if C.sizeof(C.c_void_p) == 8 else 'GetWindowLongW', [W.HWND, C.c_int], C.c_ssize_t)
set_style = bind(user, 'SetWindowLongPtrW' if C.sizeof(C.c_void_p) == 8 else 'SetWindowLongW', [W.HWND, C.c_int, C.c_ssize_t], C.c_ssize_t)
ancestor = bind(user, 'GetAncestor', [W.HWND, W.UINT], W.HWND)
set_pos = bind(user, 'SetWindowPos', [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL)
query_notification = bind(shell, 'SHQueryUserNotificationState', [C.POINTER(C.c_int)], C.c_long)

class MonitorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]
get_monitor = bind(user, 'GetMonitorInfoW', [W.HANDLE, C.POINTER(MonitorInfo)], W.BOOL)
MONITORPROC = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
enum_monitors = bind(user, 'EnumDisplayMonitors', [W.HDC, C.c_void_p, MONITORPROC, W.LPARAM], W.BOOL)

@native_pixels
def all_monitors():
    """Active display rectangles in physical desktop pixels, with effective DPI."""
    result = []
    try:
        dpi_fn = bind(C.WinDLL('shcore'), 'GetDpiForMonitor', [W.HANDLE, C.c_int, C.POINTER(W.UINT), C.POINTER(W.UINT)], C.c_long)
    except (OSError, AttributeError):
        dpi_fn = None

    @MONITORPROC
    def visit(handle, dc, rect, data):
        dpi_x, dpi_y = (W.UINT(96), W.UINT(96))
        if dpi_fn and dpi_fn(handle, 0, C.byref(dpi_x), C.byref(dpi_y)) != 0:
            dpi_x.value = 96
        r = rect.contents
        result.append((r.left, r.top, r.right, r.bottom, dpi_x.value))
        return True
    if not enum_monitors(None, None, visit, 0):
        raise C.WinError(C.get_last_error())
    if not result:
        raise RuntimeError('No active display found.')
    return result

def monitor_rect():
    info = MonitorInfo()
    info.size = C.sizeof(info)
    if not get_monitor(monitor_from(foreground(), 2), C.byref(info)):
        raise C.WinError(C.get_last_error())
    return info.monitor

def dashboard_bounds(scale):
    info = MonitorInfo()
    info.size = C.sizeof(info)
    if not get_monitor(monitor_from(foreground(), 2), C.byref(info)):
        raise C.WinError(C.get_last_error())
    work = info.work
    width = min(round(820 * scale), work.right - work.left - 60)
    height = min(round(720 * scale), work.bottom - work.top - 90)
    return (width, height, work.left + (work.right - work.left - width) // 2, work.top + 20)

def position_dashboard(root, x, y):
    set_pos(int(root.wm_frame(), 16), None, x, y, 0, 0, 1 | 4 | 16)
SUBCLASSPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM, C.c_size_t, C.c_size_t)
comctl = C.WinDLL('comctl32', use_last_error=True)
set_subclass = bind(comctl, 'SetWindowSubclass', [W.HWND, SUBCLASSPROC, C.c_size_t, C.c_size_t], W.BOOL)
remove_subclass = bind(comctl, 'RemoveWindowSubclass', [W.HWND, SUBCLASSPROC, C.c_size_t], W.BOOL)
def_subclass = bind(comctl, 'DefSubclassProc', [W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t)

class WindowControls:
    """Disable the titlebar X and route native close requests to the app overlay."""

    def __init__(self, root, request_close, end_session):
        self.root, self.request_close, self.end_session = (root, request_close, end_session)
        self.hwnd, self.queued = (None, False)
        self.callback = SUBCLASSPROC(self.dispatch)

    def attach(self):
        hwnd = int(self.root.wm_frame(), 16)
        if self.hwnd == hwnd:
            return
        self.detach()
        if not set_subclass(hwnd, self.callback, 621, 0):
            raise C.WinError(C.get_last_error())
        self.hwnd = hwnd
        menu = bind(user, 'GetSystemMenu', [W.HWND, W.BOOL], W.HMENU)(hwnd, False)
        bind(user, 'EnableMenuItem', [W.HMENU, W.UINT, W.UINT], W.UINT)(menu, 61536, 1)
        bind(user, 'DrawMenuBar', [W.HWND], W.BOOL)(hwnd)
        try:
            dwm = C.WinDLL('dwmapi')
            flag = W.BOOL(True)
            bind(dwm, 'DwmSetWindowAttribute', [W.HWND, W.DWORD, C.c_void_p, W.DWORD], C.c_long)(hwnd, 20, C.byref(flag), C.sizeof(flag))
        except (OSError, AttributeError):
            pass

    def dispatch(self, hwnd, msg, wp, lp, subclass_id, data):
        if msg == 132:
            hit = def_subclass(hwnd, msg, wp, lp)
            return 0 if hit == 20 else hit
        if msg in (161, 162, 163) and wp == 20:
            return 0
        if msg == 16 or (msg == 274 and wp & 65520 == 61536):
            if not self.queued:
                self.queued = True
                self.root.after_idle(self.confirm_once)
            return 0
        if msg == 274 and wp & 65520 == 61488:
            return 0
        if msg == 17:
            return 1
        if msg == 22 and wp:
            self.root.after_idle(self.end_session)
            return 0
        return def_subclass(hwnd, msg, wp, lp)

    def confirm_once(self):
        try:
            self.request_close()
        finally:
            self.queued = False

    def detach(self):
        if self.hwnd:
            remove_subclass(self.hwnd, self.callback, 621)
            self.hwnd = None

@native_pixels
def fullscreen():
    state = C.c_int()
    if query_notification(C.byref(state)) == 0 and state.value in (1, 2, 3):
        return True
    hwnd = foreground()
    if not hwnd:
        return True
    name = C.create_unicode_buffer(256)
    get_class(hwnd, name, 256)
    if name.value in ('Progman', 'WorkerW', 'Shell_TrayWnd'):
        return False
    rect = W.RECT()
    if not get_rect(hwnd, C.byref(rect)):
        return True
    target = monitor_rect()
    caption = get_style(hwnd, -16) & 12582912
    return not caption and (rect.left <= target.left + 2 and rect.top <= target.top + 2 and (rect.right >= target.right - 2) and (rect.bottom >= target.bottom - 2))

class DataBlob(C.Structure):
    _fields_ = [('size', W.DWORD), ('data', C.POINTER(C.c_ubyte))]

crypt32 = C.WinDLL('crypt32', use_last_error=True)
protect_data = bind(crypt32, 'CryptProtectData', [C.POINTER(DataBlob), W.LPCWSTR, C.POINTER(DataBlob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DataBlob)], W.BOOL)
unprotect_data = bind(crypt32, 'CryptUnprotectData', [C.POINTER(DataBlob), C.c_void_p, C.POINTER(DataBlob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DataBlob)], W.BOOL)
local_free = bind(kernel, 'LocalFree', [C.c_void_p], C.c_void_p)

def credential_path(username):
    return DATA / 'credentials' / (hashlib.sha256(username.encode('utf-8')).hexdigest() + '.bin')

def protect_secret(data, decrypt=False):
    """DPAPI protects the local blob for this Windows user; it stores no app key."""
    buffer = (C.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = DataBlob(len(data), buffer), DataBlob()
    function = unprotect_data if decrypt else protect_data
    try:
        if not function(C.byref(source), None, None, None, None, 1, C.byref(output)):
            raise C.WinError(C.get_last_error())
        return C.string_at(output.data, output.size)
    finally:
        C.memset(buffer, 0, len(data))
        if output.data:
            C.memset(output.data, 0, output.size)
            local_free(output.data)

def stop_sound():
    try:
        winsound.PlaySound(None, 0)
    except RuntimeError:
        pass

def read_key(username):
    try:
        blob = credential_path(username).read_bytes()
    except FileNotFoundError:
        return None
    return protect_secret(blob, decrypt=True).decode('utf-8')

def save_key(username, key):
    atomic_write(credential_path(username), protect_secret(key.encode('utf-8')))

def delete_key(username):
    credential_path(username).unlink(missing_ok=True)

class InstanceLock:

    def __init__(self):
        create = bind(kernel, 'CreateMutexW', [C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE)
        self.handle = create(None, False, 'Local\\E621TicketBot.Monitor')
        if not self.handle:
            raise C.WinError(C.get_last_error())
        if C.get_last_error() == 183:
            self.close()
            raise RuntimeError('The monitor is already running. Close its window before starting another copy.')

    def close(self):
        if self.handle:
            bind(kernel, 'CloseHandle', [W.HANDLE], W.BOOL)(self.handle)
            self.handle = None

class StopSignal:
    """Current desktop session's stop request, usable by the hidden startup app."""

    def __init__(self):
        self.handle = bind(kernel, 'CreateEventW', [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE)(None, True, False, 'Local\\E621TicketBot.Stop')
        if not self.handle:
            raise C.WinError(C.get_last_error())

    def is_set(self):
        return bind(kernel, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(self.handle, 0) == 0

    def close(self):
        if self.handle:
            bind(kernel, 'CloseHandle', [W.HANDLE], W.BOOL)(self.handle)
            self.handle = None

def request_stop():
    handle = bind(kernel, 'OpenEventW', [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE)(2, False, 'Local\\E621TicketBot.Stop')
    if not handle:
        return False
    try:
        if not bind(kernel, 'SetEvent', [W.HANDLE], W.BOOL)(handle):
            raise C.WinError(C.get_last_error())
        return True
    finally:
        bind(kernel, 'CloseHandle', [W.HANDLE], W.BOOL)(handle)

@lru_cache(maxsize=1)
def chime_bytes():
    """Synthesize a three-second bell phrase directly as in-memory PCM audio."""
    rate, duration = 32000, 3
    notes = ((0.0, 523.251), (0.20, 659.255), (0.40, 783.991), (0.65, 1046.502))
    samples = array('h')
    for i in range(rate * duration):
        t = i / rate
        value = 0.0
        for start, frequency in notes:
            age = t - start
            if age >= 0:
                envelope = min(1.0, age / 0.008) * math.exp(-3.1 * age)
                tone = math.sin(math.tau * frequency * age) + 0.20 * math.sin(math.tau * frequency * 2.01 * age)
                value += 0.22 * envelope * tone
        value *= min(1.0, (duration - t) / 0.15)
        samples.append(round(max(-1.0, min(1.0, value)) * 32767))
    if sys.byteorder != 'little':
        samples.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())
    return buffer.getvalue()

def sound():
    # SND_MEMORY cannot be asynchronous; a worker keeps Tk responsive.
    data = chime_bytes()
    def play():
        try:
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except RuntimeError:
            pass
    threading.Thread(target=play, daemon=True).start()

def toast(count, test=False):
    resource_path('assets/e621-ticket-bot.png')
    path = resource_path('toast.ps1')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path), '-Count', str(count), '-TestAlert', str(int(test))], capture_output=True, timeout=20, creationflags=134217728)
    if result.returncode:
        raise RuntimeError('Windows did not accept the toast notification. Check Windows notification settings.')
WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
default_proc = bind(user, 'DefWindowProcW', [W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t)

@WNDPROC
def halo_proc(hwnd, message, wparam, lparam):
    if message == 132:
        return -1
    if message == 33:
        return 3
    return default_proc(hwnd, message, wparam, lparam)

class WindowClass(C.Structure):
    _fields_ = [('style', W.UINT), ('proc', WNDPROC), ('class_extra', C.c_int), ('window_extra', C.c_int), ('instance', W.HINSTANCE), ('icon', W.HICON), ('cursor', W.HANDLE), ('brush', W.HBRUSH), ('menu', W.LPCWSTR), ('name', W.LPCWSTR)]
register_class = bind(user, 'RegisterClassW', [C.POINTER(WindowClass)], W.WORD)
create_window = bind(user, 'CreateWindowExW', [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p], W.HWND)
layer_alpha = bind(user, 'SetLayeredWindowAttributes', [W.HWND, W.DWORD, W.BYTE, W.DWORD], W.BOOL)
destroy_window = bind(user, 'DestroyWindow', [W.HWND], W.BOOL)
get_module = bind(kernel, 'GetModuleHandleW', [W.LPCWSTR], W.HMODULE)
gdi = C.WinDLL('gdi32', use_last_error=True)
solid_brush = bind(gdi, 'CreateSolidBrush', [W.DWORD], W.HBRUSH)
get_dc = bind(user, 'GetDC', [W.HWND], W.HDC)
release_dc = bind(user, 'ReleaseDC', [W.HWND, W.HDC], C.c_int)
fill_rect = bind(user, 'FillRect', [W.HDC, C.POINTER(W.RECT), W.HBRUSH], C.c_int)
_halo_classes = []
_halo_brushes = []

def register_halo_classes():
    if _halo_classes:
        return
    for red, green, blue in PALETTE:
        _halo_brushes.append(solid_brush(red | green << 8 | blue << 16))
    for name, rgb in (('E621HaloBlue', 10310657),):
        wc = WindowClass()
        wc.proc, wc.instance, wc.brush, wc.name = (halo_proc, get_module(None), solid_brush(rgb), name)
        if not register_class(C.byref(wc)):
            raise C.WinError(C.get_last_error())
        _halo_classes.append(wc)

class _BandedHalo:

    def __init__(self, root, seconds, width):
        self.root, self.seconds, self.width = (root, seconds, width)
        self.windows, self.started = ([], 0)
        self.paint_plans, self.monitor_bounds = ([], [])

    @native_pixels
    def show(self):
        self.hide()
        register_halo_classes()
        self.monitor_bounds = all_monitors()
        for left, top, right, bottom, dpi in self.monitor_bounds:
            thickness = max(10, round(self.width * dpi / 96))
            for (x, y, w, h), alpha, plan in bands(right - left, bottom - top, thickness):
                hwnd = create_window(524288 | 32 | 128 | 134217728, 'E621HaloBlue', '', 2147483648, left + x, top + y, w, h, None, None, get_module(None), None)
                if not hwnd:
                    self.hide()
                    raise C.WinError(C.get_last_error())
                self.windows.append((hwnd, alpha))
                self.paint_plans.append((hwnd, [(W.RECT(*r), p) for r, p in plan]))
                layer_alpha(hwnd, 0, 0, 2)
                set_pos(hwnd, W.HWND(-1), left + x, top + y, w, h, 16 | 64)
        self.started = time.monotonic()

    @native_pixels
    def tick(self):
        if not self.windows:
            return
        elapsed = time.monotonic() - self.started
        if elapsed >= self.seconds:
            self.hide()
            return
        fade = min(1, elapsed / 0.15, (self.seconds - elapsed) / 0.35)
        for hwnd, plan in self.paint_plans:
            dc = get_dc(hwnd)
            try:
                for rect, position in plan:
                    fill_rect(dc, C.byref(rect), _halo_brushes[color_index(position, elapsed)])
            finally:
                release_dc(hwnd, dc)
        for hwnd, alpha in self.windows:
            layer_alpha(hwnd, 0, int(max(0, alpha * fade) * 255), 2)

    def hide(self):
        for hwnd, _ in self.windows:
            destroy_window(hwnd)
        self.windows.clear()
        self.paint_plans.clear()

class BitmapHeader(C.Structure):
    _fields_ = [('size', W.DWORD), ('width', W.LONG), ('height', W.LONG), ('planes', W.WORD), ('bits', W.WORD), ('compression', W.DWORD), ('image_size', W.DWORD), ('xppm', W.LONG), ('yppm', W.LONG), ('used', W.DWORD), ('important', W.DWORD)]

class BitmapInfo(C.Structure):
    _fields_ = [('header', BitmapHeader), ('colors', W.DWORD * 3)]

class BlendFunction(C.Structure):
    _fields_ = [('operation', W.BYTE), ('flags', W.BYTE), ('alpha', W.BYTE), ('format', W.BYTE)]
create_memory_dc = bind(gdi, 'CreateCompatibleDC', [W.HDC], W.HDC)
delete_dc = bind(gdi, 'DeleteDC', [W.HDC], W.BOOL)
create_dib = bind(gdi, 'CreateDIBSection', [W.HDC, C.POINTER(BitmapInfo), W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP)
select_object = bind(gdi, 'SelectObject', [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
delete_object = bind(gdi, 'DeleteObject', [W.HGDIOBJ], W.BOOL)
update_layered = bind(user, 'UpdateLayeredWindow', [W.HWND, W.HDC, C.POINTER(W.POINT), C.POINTER(W.SIZE), W.HDC, C.POINTER(W.POINT), W.DWORD, C.POINTER(BlendFunction), W.DWORD], W.BOOL)

class LayeredSurface:

    def __init__(self, x, y, texture):
        self.texture = texture
        _, _, w, h = texture.rect
        self.origin, self.size = (W.POINT(x, y), W.SIZE(w, h))
        self.hwnd = self.dc = self.bitmap = self.previous = None
        try:
            self.hwnd = create_window(524288 | 32 | 128 | 134217728, 'E621HaloBlue', '', 2147483648, x, y, w, h, None, None, get_module(None), None)
            self.dc = create_memory_dc(None)
            info = BitmapInfo()
            info.header.size = C.sizeof(BitmapHeader)
            info.header.width, info.header.height, info.header.planes, info.header.bits = (w, -h, 1, 32)
            self.bits = C.c_void_p()
            self.bitmap = create_dib(self.dc, C.byref(info), 0, C.byref(self.bits), None, 0)
            if not self.hwnd or not self.dc or (not self.bitmap) or (not self.bits):
                raise C.WinError(C.get_last_error())
            self.previous = select_object(self.dc, self.bitmap)
            self.paint(0, 0)
            set_pos(self.hwnd, W.HWND(-1), x, y, w, h, 16 | 64)
        except Exception:
            self.close()
            raise

    def paint(self, elapsed, opacity):
        pixels = self.texture.frame(elapsed)
        C.memmove(self.bits, pixels, len(pixels))
        blend = BlendFunction(0, 0, round(255 * max(0, min(1, opacity))), 1)
        source = W.POINT(0, 0)
        if not update_layered(self.hwnd, None, C.byref(self.origin), C.byref(self.size), self.dc, C.byref(source), 0, C.byref(blend), 2):
            raise C.WinError(C.get_last_error())

    def close(self):
        if self.dc and self.previous:
            select_object(self.dc, self.previous)
        if self.bitmap:
            delete_object(self.bitmap)
        if self.dc:
            delete_dc(self.dc)
        if self.hwnd:
            destroy_window(self.hwnd)
        self.hwnd = self.dc = self.bitmap = self.previous = None

class Halo(_BandedHalo):

    def __init__(self, root, seconds, width):
        super().__init__(root, seconds, width)
        self.surfaces = []
        self.legacy = False

    @native_pixels
    def show(self):
        self.hide()
        if Image is None:
            raise GraphicsUnavailable('Pillow graphics support is required before starting the bot.')
        self.legacy = False
        register_halo_classes()
        self.monitor_bounds = all_monitors()
        try:
            for left, top, right, bottom, dpi in self.monitor_bounds:
                thickness = max(10, round(self.width * dpi / 96))
                for edge in range(4):
                    texture = EdgeTexture(right - left, bottom - top, thickness, edge)
                    x, y, _, _ = texture.rect
                    surface = LayeredSurface(left + x, top + y, texture)
                    self.surfaces.append(surface)
                    self.windows.append((surface.hwnd, 1))
        except Exception:
            self.hide()
            raise
        self.started = time.monotonic()

    @native_pixels
    def tick(self):
        if self.legacy:
            return super().tick()
        if not self.surfaces:
            return
        elapsed = time.monotonic() - self.started
        if elapsed >= self.seconds:
            self.hide()
            return
        fade = min(1, elapsed / 0.15, (self.seconds - elapsed) / 0.35)
        for surface in self.surfaces:
            surface.paint(elapsed, fade)

    def hide(self):
        if self.legacy:
            return super().hide()
        for surface in self.surfaces:
            surface.close()
        self.surfaces.clear()
        self.windows.clear()
        self.paint_plans.clear()

# Cached UI indicators

class SmoothIndicator:

    def __init__(self, canvas, x, y, size, color='#FCBF31'):
        if Image is None or ImageTk is None:
            raise GraphicsUnavailable('Pillow graphics support is required before starting the bot.')
        self.canvas, self.size, self.color = (canvas, size, color)
        self.cache = {}
        self.im, self.draw, self.tk = (Image, ImageDraw, ImageTk)
        self.smooth = True
        self.item = canvas.create_image(x, y)

    def frame(self, kind, phase, size, color):
        key = (kind, phase, size, color)
        if key not in self.cache:
            factor = 4
            image = self.im.new('RGBA', (size * factor, size * factor))
            draw = self.draw.Draw(image)
            center = size * factor / 2
            if kind == 'spinner':
                radius = size * 0.34 * factor
                width = max(1, round(size * 0.11 * factor))
                start = phase * 10
                outer = radius + width / 2
                draw.arc((center - outer, center - outer, center + outer, center + outer), start, start + 250, fill=color, width=width)
                for angle in (start, start + 250):
                    x = center + radius * math.cos(math.radians(angle))
                    y = center + radius * math.sin(math.radians(angle))
                    draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)
            else:
                radius = size * (0.16 + 0.025 * math.sin(phase * math.tau / 24)) * factor
                draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=color)
            image = image.resize((size, size), self.im.Resampling.LANCZOS)
            if len(self.cache) > 256:
                self.cache.clear()
            self.cache[key] = self.tk.PhotoImage(image, master=self.canvas)
        return self.cache[key]

    def update(self, kind, now, x, y, live=True, size=None, color=None):
        size = max(8, round(size or self.size))
        color = color or self.color
        phase = int(now * 22) % 36 if kind == 'spinner' else int(now * 8) % 24 if live else 0
        state = (kind, phase, size, color, x, y)
        if state == getattr(self, 'last_state', None):
            return
        self.last_state = state
        self.current = self.frame(kind, phase, size, color)
        self.canvas.coords(self.item, x, y)
        self.canvas.itemconfigure(self.item, image=self.current)

# Dashboard and in-app overlays

BG, CARD, TEXT, MUTED = ('#101722', '#192332', '#EDF2F8', '#A8B6C8')
BLUE, YELLOW, RED = ('#01549D', '#FCBF31', '#712F38')

def fit_text(text, measure, width):
    text = ' '.join(str(text).split())
    if measure(text) <= width:
        return text
    if measure('...') > width:
        return ''
    lo, hi = (0, len(text))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + '...') <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + '...'

class Dashboard:

    def __init__(self, app):
        self.app, self.root = (app, app.root)
        self.widget_options = {}
        self.item_options = {}
        root = self.root
        root.configure(bg=BG)
        root.resizable(False, False)
        self.scale = min(1.15, max(1, root.winfo_fpixels('1i') / 96))
        self.p = lambda n: round(n * self.scale)
        p = self.p
        self.w, self.h, x, y = dashboard_bounds(self.scale)
        w, h = (self.w, self.h)
        root.geometry(f'{w}x{h}')
        root.minsize(w, h)
        root.maxsize(w, h)
        root.after_idle(lambda: position_dashboard(root, x, y))

        def font(size, bold=False):
            return tkfont.Font(root, family='Segoe UI', size=-p(size), weight='bold' if bold else 'normal')
        self.title_font, self.font, self.bold, self.small = (font(21, True), font(12), font(12, True), font(11))
        self.welcome_font, self.welcome_subfont = (font(27, True), font(16))
        c = self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0, width=w, height=h, takefocus=False)
        c.place(x=0, y=0, relwidth=1, relheight=1)
        root.bind('<MouseWheel>', lambda e: 'break')
        self.text = lambda x, y, t='', f=None, color=TEXT, anchor='w': c.create_text(x, y, text=t, font=f or self.font, fill=color, anchor=anchor)
        self.text(p(20), p(24), "Wisp's e621 Mod Ticket Bot", self.title_font)
        self.text(p(20), p(49), 'Pending [Unclaimed] Moderator Tickets', self.small, YELLOW)
        self.avatar_x = w - p(265)
        self.avatar = self.text(self.avatar_x, p(32), '', self.title_font, YELLOW, 'center')
        self.account_label = self.text(w - p(225), p(22), 'Account', self.small, MUTED, 'w')
        self.account_name = self.text(w - p(225), p(39), '', self.bold, TEXT, 'w')
        self.login_button = self.button('Login', self.account_action, CARD, YELLOW, w - p(83), p(20), p(63), p(29))
        self.run_button = self.button('Login to Start', app.toggle_running, '#286345', TEXT, p(20), p(78), p(113), p(28))
        self.pause_button = self.button('Pause', app.toggle_pause, CARD, TEXT, p(143), p(78), p(80), p(28))
        self.text(p(241), p(92), 'Refresh every', color=MUTED)
        self.interval = tk.StringVar(value=str(app.config['poll_seconds']))
        self.interval_entry = tk.Entry(root, textvariable=self.interval, font=self.font, bg=CARD, fg=TEXT, disabledbackground=CARD, disabledforeground=MUTED, insertbackground=YELLOW, relief='flat', justify='center')
        self.interval_entry.place(x=p(323), y=p(79), width=p(49), height=p(27))
        self.interval_entry.bind('<Return>', lambda e: self.apply_interval())
        self.text(p(380), p(92), 'seconds', color=MUTED)
        self.apply_button = self.button('Apply', self.apply_interval, CARD, YELLOW, p(435), p(78), p(62), p(28))
        self.interval_error_label = self.text(p(510), p(92), '', self.small, '#FFB4B4')
        c.itemconfigure(self.interval_error_label, width=max(100, w - p(530)))
        self.interval.trace_add('write', lambda *_: self.show_interval_error(''))
        self.bar_box = (p(20), p(119), w - p(20), p(154))
        c.create_rectangle(*self.bar_box, fill=CARD, outline='')
        self.bar_fill = c.create_rectangle(*self.bar_box, fill=BLUE, outline='')
        self.refresh_indicator = SmoothIndicator(c, p(39), p(136), p(22))
        self.activity_label = self.text(p(59), p(137), '', self.bold)
        self.status_label = self.text(p(20), p(174))
        self.last_found_label = self.text(p(20), p(196), '', self.bold)
        self.last_label = self.text(w - p(20), p(196), '', self.small, MUTED, 'e')
        self.count_label = self.text(p(20), p(218), '', self.bold, YELLOW)
        self.text(p(20), p(248), 'RECENT TICKETS', self.bold, YELLOW)
        self.history_caption = self.text(w - p(20), p(248), 'Last 10 real detections · saved on this PC', self.small, MUTED, 'e')
        top, bottom = (p(265), h - p(75))
        self.row_height = (bottom - top) / 10
        px = min(p(12), max(8, int(self.row_height / 2) - 3))
        self.row_font = tkfont.Font(root, family='Segoe UI', size=-px)
        self.row_bold = tkfont.Font(root, family='Segoe UI', size=-px, weight='bold')
        self.ticket_links = {}
        self.ticket_underlines = {}
        self.hovered_ticket = None
        self.rows = []
        for i in range(10):
            ry = top + i * self.row_height
            c.create_rectangle(p(20), ry, w - p(20), ry + self.row_height - 1, fill=CARD if i % 2 == 0 else '#151E2B', outline='')
            hit = c.create_rectangle(p(30), ry + 1, w - p(30), ry + self.row_height / 2, fill=CARD if i % 2 == 0 else '#151E2B', outline='')
            title = self.text(p(30), ry + 2, '', self.row_bold, YELLOW, 'nw')
            c.itemconfigure(title, state='disabled')
            self.ticket_underlines[title] = c.create_line(0, 0, 0, 0, fill=YELLOW, width=1, state='hidden')
            reason = self.text(p(30), ry + self.row_height / 2, '', self.row_font, TEXT, 'nw')
            self.rows.append((title, reason))
            c.tag_bind(hit, '<Enter>', lambda e, item=title: self.hover_ticket(item, True))
            c.tag_bind(hit, '<Leave>', lambda e, item=title: self.hover_ticket(item, False))
            c.tag_bind(hit, '<Button-1>', lambda e, item=title: self.open_ticket(item))
        self.settings_button = self.button('Settings', self.show_settings, CARD, TEXT, p(20), h - p(55), p(110), p(35))
        self.settings_menu = None
        self.config_menu = None
        root.bind('<Button-1>', self.dismiss_settings_outside, add='+')
        root.bind('<Escape>', lambda e: self.close_settings(), add='+')
        self.stop_button = self.button('Close Bot', app.request_close, RED, TEXT, w - p(125), h - p(55), p(105), p(35))
        self.current_avatar = None
        self.overlay = None
        self.overlay_ready_at = None
        self.overlay_mode = None
        self.animation_job = None
        self.login_form = None
        self.close_overlay = None
        self.refresh_tickets()
        self.base_w, self.base_h = (w, h)
        self.control_layout = {widget: tuple((float(widget.place_info()[k]) for k in ('x', 'y', 'width', 'height'))) for widget in root.winfo_children() if widget is not c and widget.winfo_manager() == 'place'}
        root.bind('<Configure>', self.resize_content, add='+')

    def resize_content(self, event):
        if event.widget is not self.root or event.width < 100 or event.height < 100:
            return
        if (event.width, event.height) == (self.w, self.h):
            return
        sx, sy = (event.width / self.w, event.height / self.h)
        self.canvas.scale('all', 0, 0, sx, sy)
        self.bar_box = tuple((v * (sx if i % 2 == 0 else sy) for i, v in enumerate(self.bar_box)))
        self.avatar_x *= sx
        self.w, self.h = (event.width, event.height)
        self.canvas.itemconfigure(self.interval_error_label, width=max(100, self.w - self.p(530) * self.w / self.base_w))
        for widget, (x, y, w, h) in self.control_layout.items():
            widget.place(x=round(x * self.w / self.base_w), y=round(y * self.h / self.base_h), width=round(w * self.w / self.base_w), height=round(h * self.h / self.base_h))
        self.refresh_tickets()

    def button(self, text, command, bg, fg, x, y, w, h):
        button = tk.Button(self.root, text=text, command=command, font=self.bold, bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief='flat', borderwidth=0, cursor='hand2', highlightthickness=1, highlightbackground=bg, highlightcolor=YELLOW)
        button.place(x=x, y=y, width=w, height=h)
        return button

    def update_widget(self, widget, **options):
        saved = self.widget_options.setdefault(widget, {})
        changes = {key: value for key, value in options.items() if saved.get(key) != value}
        if changes:
            widget.configure(**changes)
            saved.update(changes)

    def update_item(self, item, **options):
        saved = self.item_options.setdefault(item, {})
        changes = {key: value for key, value in options.items() if saved.get(key) != value}
        if changes:
            self.canvas.itemconfigure(item, **changes)
            saved.update(changes)

    def apply_interval(self):
        if self.app.change_interval(self.interval.get()):
            self.interval.set(str(self.app.config['poll_seconds']))

    def show_interval_error(self, message):
        self.canvas.itemconfigure(self.interval_error_label, text=message)

    def account_action(self):
        self.app.logout() if self.app.authenticated else self.app.show_login()

    def show_settings(self):
        if self.settings_menu is not None:
            self.close_settings()
            return
        allowed = (self.app.authenticated or self.app.args.test) and (not self.app.schedule.testing)
        if not allowed or self.app.login_overlay_active or self.app.confirming:
            return
        menu = self.settings_menu = tk.Frame(self.root, bg=CARD, highlightbackground='#354459', highlightthickness=1)
        menu.place(x=self.settings_button.winfo_x(), y=self.settings_button.winfo_y() - 110, width=175, height=106)
        menu.lift()
        for label, command in (('Alert Test', self.app.test_alert), ('Ticket Test', self.app.request_ticket_test)):

            def action(fn=command):
                self.close_settings()
                fn()
            tk.Button(menu, text=label, command=action, anchor='w', font=self.font, bg=CARD, fg=TEXT, activebackground=BLUE, activeforeground=TEXT, relief='flat', padx=12).pack(fill='x', ipady=2)
        self.config_button = tk.Button(menu, text='Config  ▶', command=self.show_config, anchor='w', font=self.font, bg=CARD, fg=TEXT, activebackground=BLUE, activeforeground=TEXT, relief='flat', padx=12)
        self.config_button.pack(fill='x', ipady=2)
        self.config_button.bind('<Enter>', lambda e: self.show_config())

    def show_config(self):
        if self.settings_menu is None or self.config_menu is not None:
            return
        panel = self.config_menu = tk.Frame(self.root, bg=CARD, highlightbackground='#354459', highlightthickness=1)
        panel.place(x=self.settings_menu.winfo_x() + 174, y=self.settings_menu.winfo_y() + 62, width=182, height=77)
        panel.lift()
        self.alert_options = {}
        for label, key in (('Sound', 'sound_enabled'), ('Halo Effect', 'halo_enabled')):
            variable = tk.BooleanVar(value=self.app.config[key])

            def toggle(setting=key, var=variable):
                if not self.app.set_alert_option(setting, var.get()):
                    var.set(self.app.config[setting])
            control = tk.Checkbutton(panel, text=label, variable=variable, command=toggle, anchor='w', font=self.font, bg=CARD, fg=TEXT, selectcolor=BG, activebackground=CARD, activeforeground=YELLOW, relief='flat', padx=10)
            control.pack(fill='x', ipady=4)
            self.alert_options[key] = (variable, control)

    def close_settings(self):
        for name in ('config_menu', 'settings_menu'):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.destroy()
                setattr(self, name, None)

    def dismiss_settings_outside(self, event):
        if self.settings_menu is None:
            return
        widget = event.widget
        while widget is not None:
            if widget in (self.settings_menu, self.config_menu, self.settings_button):
                return
            widget = getattr(widget, 'master', None)
        self.close_settings()

    def refresh_tickets(self):
        rows = self.app.recent.rows[:10]
        self.ticket_links.clear()
        for i, (title, reason) in enumerate(self.rows):
            if i < len(rows):
                ticket_id, text, when = rows[i]
                heading = f"#{ticket_id}  ·  {when.astimezone().strftime('%d/%m/%y, %H:%M:%S')}"
                text = text or '[No reason supplied in the response]'
                if type(ticket_id) is int and ticket_id > 0:
                    self.ticket_links[title] = ticket_id
            else:
                heading = 'No real tickets recorded yet.' if i == 0 else ''
                text = ''
            self.canvas.itemconfigure(title, text=fit_text(heading, self.row_bold.measure, self.w - self.p(60)), font=self.row_bold)
            self.canvas.itemconfigure(reason, text=fit_text(text, self.row_font.measure, self.w - self.p(60)))
        if self.hovered_ticket is not None:
            self.hover_ticket(self.hovered_ticket, True)

    def hover_ticket(self, item, entered):
        active = entered and item in self.ticket_links and (not (self.app.login_overlay_active or self.app.confirming))
        if self.hovered_ticket is not None:
            self.canvas.itemconfigure(self.ticket_underlines[self.hovered_ticket], state='hidden')
        self.hovered_ticket = item if active else None
        if active:
            x, y = self.canvas.coords(item)
            baseline = y + self.row_bold.metrics('ascent') + 1
            width = self.row_bold.measure(self.canvas.itemcget(item, 'text'))
            line = self.ticket_underlines[item]
            self.canvas.coords(line, x, baseline, x + width, baseline)
            self.canvas.itemconfigure(line, state='disabled')
        self.canvas.configure(cursor='hand2' if active else '')

    def open_ticket(self, item):
        ticket_id = self.ticket_links.get(item)
        if ticket_id and (not (self.app.login_overlay_active or self.app.confirming)) and (self.app.authenticated or self.app.args.test):
            webbrowser.open(f'https://e621.net/tickets/{ticket_id}')

    def show_close_confirmation(self, on_result):
        self.close_settings()
        if self.hovered_ticket is not None:
            self.hover_ticket(self.hovered_ticket, False)
        self.close_previous_focus = self.root.focus_get()
        self.close_previous_grab = self.root.grab_current()
        self.close_result = on_result
        cover = self.close_overlay = tk.Canvas(self.root, bg=BG, highlightthickness=0, takefocus=False)
        cover.place(x=0, y=0, relwidth=1, relheight=1)
        self.root.tk.call('raise', cover._w)
        card = tk.Frame(cover, bg=CARD, padx=self.p(28), pady=self.p(26))
        card.place(relx=0.5, rely=0.5, anchor='center')
        tk.Label(card, text='Close Bot?', font=self.title_font, bg=CARD, fg=TEXT).pack(anchor='w', pady=(0, self.p(14)))
        self.close_message = "Close Wisp's e621 Mod Ticket Bot?\n\nTicket monitoring will stop until you launch it again."
        tk.Label(card, text=self.close_message, font=self.font, bg=CARD, fg=TEXT, justify='left', wraplength=self.p(380)).pack(anchor='w')
        actions = tk.Frame(card, bg=CARD)
        actions.pack(fill='x', pady=(self.p(24), 0))
        self.close_cancel_button = tk.Button(actions, text='Cancel', command=lambda: self.answer_close(False), font=self.bold, bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT, relief='flat', padx=20, pady=9)
        self.close_confirm_button = tk.Button(actions, text='Close Bot', command=lambda: self.answer_close(True), font=self.bold, bg=RED, fg=TEXT, activebackground=RED, activeforeground=TEXT, relief='flat', padx=20, pady=9)
        self.close_cancel_button.pack(side='left')
        self.close_confirm_button.pack(side='right')
        for button, other in ((self.close_cancel_button, self.close_confirm_button), (self.close_confirm_button, self.close_cancel_button)):
            button.bind('<Escape>', lambda e: self.answer_close(False))
            button.bind('<Return>', lambda e, b=button: (b.invoke(), 'break')[1])
            for key in ('<Tab>', '<Shift-Tab>'):
                button.bind(key, lambda e, b=other: (b.focus_set(), 'break')[1])
        cover.grab_set()
        self.close_cancel_button.focus_set()
        self.tick(time.monotonic())

    def answer_close(self, accepted):
        if self.close_overlay is None:
            return
        self.close_overlay.grab_release()
        self.close_overlay.destroy()
        self.close_overlay = None
        self.close_result(accepted)
        if not self.app.closing:
            self.tick(time.monotonic())
            grab = self.overlay if self.overlay is not None else self.close_previous_grab
            focus = self.close_previous_focus
            if focus is None or not focus.winfo_exists():
                focus = self.login_key if self.overlay_mode == 'form' else self.overlay
            for widget, action in ((grab, 'grab_set'), (focus, 'focus_set')):
                if widget is not None and widget.winfo_exists():
                    getattr(widget, action)()

    def make_overlay(self, mode):
        self.close_settings()
        self.end_login_overlay()
        self.app.login_overlay_active = True
        self.overlay_mode = mode
        self.overlay_ready_at = None
        self.overlay = tk.Canvas(self.root, bg=BG, highlightthickness=0, takefocus=True)
        self.overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self.root.tk.call('raise', self.overlay._w)
        self.overlay.focus_set()
        self.overlay.grab_set()
        self.overlay.update_idletasks()
        if self.close_overlay is not None:
            self.root.tk.call('raise', self.close_overlay._w)
            self.close_overlay.grab_set()
            self.close_cancel_button.focus_set()

    def show_login_form(self, error='', username=None):
        if self.overlay_mode == 'form':
            return
        self.make_overlay('form')
        form = self.login_form = tk.Frame(self.overlay, bg=CARD, padx=32, pady=28)
        form.place(relx=0.5, rely=0.5, anchor='center')
        tk.Label(form, text='Log in to e621', bg=CARD, fg=TEXT, font=self.title_font).pack(anchor='w', pady=(0, 18))
        tk.Label(form, text='Username', bg=CARD, fg=MUTED, font=self.font).pack(anchor='w')
        self.login_username = tk.Entry(form, width=32, font=self.font, bg=BG, fg=TEXT, insertbackground=YELLOW, relief='flat')
        self.login_username.insert(0, username if username is not None else self.app.config['username'])
        self.login_username.pack(fill='x', ipady=7, pady=(5, 14))
        tk.Label(form, text='API key (not your password)', bg=CARD, fg=MUTED, font=self.font).pack(anchor='w')
        self.login_key = tk.Entry(form, width=32, show='•', font=self.font, bg=BG, fg=TEXT, insertbackground=YELLOW, relief='flat')
        self.login_key.pack(fill='x', ipady=7, pady=(5, 8))
        self.login_error = tk.Label(form, text=error, bg=CARD, fg='#FFB4B4', font=self.small, wraplength=310, justify='left')
        self.login_error.pack(fill='x')
        actions = tk.Frame(form, bg=CARD)
        actions.pack(fill='x', pady=(16, 0))
        tk.Button(actions, text='Cancel', command=self.end_login_overlay, font=self.bold, bg=BG, fg=TEXT, relief='flat', padx=18, pady=9).pack(side='left')
        self.submit_button = tk.Button(actions, text='Submit', command=self.submit_login, font=self.bold, bg=BLUE, fg=YELLOW, relief='flat', padx=24, pady=9)
        self.submit_button.pack(side='right')
        for entry in (self.login_username, self.login_key):
            entry.bind('<Return>', lambda e: self.submit_login())
            entry.bind('<Escape>', lambda e: self.end_login_overlay())
        self.login_key.focus_set()
        if self.close_overlay is not None:
            self.close_cancel_button.focus_set()

    def submit_login(self):
        name, secret = (self.login_username.get().strip(), self.login_key.get().strip())
        self.login_key.delete(0, 'end')
        self.app.begin_login(name, secret, save=True)

    def begin_login_overlay(self, username):
        self.make_overlay('checking')
        self.overlay.bind('<Key>', lambda e: 'break')
        self.login_indicator = SmoothIndicator(self.overlay, 0, 0, 64)
        self.overlay_label = self.overlay.create_text(0, 0, text='Checking Your Credentials...', font=self.title_font, fill=TEXT)
        self.overlay_subtitle = self.overlay.create_text(0, 0, text='Logging You In...', font=self.welcome_subfont, fill=MUTED, state='hidden')
        self.overlay_image = None
        self.run_overlay_frame()

    def end_login_overlay(self):
        if getattr(self, 'animation_job', None):
            self.root.after_cancel(self.animation_job)
            self.animation_job = None
        if getattr(self, 'overlay', None) is not None:
            self.overlay.grab_release()
            self.overlay.destroy()
            self.overlay = None
        self.login_form = None
        self.overlay_mode = None
        self.app.login_overlay_active = False

    def run_overlay_frame(self):
        self.animation_job = None
        if self.overlay is None or self.overlay_mode == 'form':
            return
        self.animate_login_overlay(time.monotonic())
        if self.overlay is not None:
            self.animation_job = self.root.after(16, self.run_overlay_frame)

    @staticmethod
    def blend_color(front, back, amount):
        a = [int(front[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(back[i:i + 2], 16) for i in (1, 3, 5)]
        return '#' + ''.join((f'{round(x + (y - x) * amount):02x}' for x, y in zip(a, b)))

    def animate_login_overlay(self, now):
        if self.overlay is None or self.overlay_mode == 'form':
            return
        app = self.app
        w, h = (self.overlay.winfo_width(), self.overlay.winfo_height())
        cx, cy = (w / 2, h / 2)
        if app.login_busy or app.avatar_busy or now < app.account_loading_until:
            self.overlay_mode = 'checking' if app.login_busy else 'loading'
            self.overlay.itemconfigure(self.overlay_label, text='Checking Your Credentials...' if app.login_busy else 'Loading Account...')
            self.login_indicator.update('spinner', now, cx, cy - 38)
            self.overlay.coords(self.overlay_label, cx, cy + 30)
            return
        if not app.authenticated:
            self.end_login_overlay()
            return
        if self.overlay_ready_at is None:
            self.overlay_mode = 'welcome'
            self.overlay_ready_at = now
            self.overlay.itemconfigure(self.login_indicator.item, state='hidden')
            self.overlay.itemconfigure(self.overlay_label, text=fit_text(f"Welcome {app.config['username']}", self.welcome_font.measure, w - 40), font=self.welcome_font)
            if app.avatar_image:
                self.overlay_image = app.avatar_image
                self.overlay.create_image(cx, cy - 82, image=self.overlay_image, tags='portrait')
            else:
                self.overlay.create_oval(cx - 34, cy - 116, cx + 34, cy - 48, fill=CARD, outline=YELLOW, tags='portrait')
                self.overlay.create_text(cx, cy - 82, text=app.config['username'][:1].upper(), font=self.title_font, fill=YELLOW, tags='portrait')
            self.portrait_center = (cx, cy)
        previous_x, previous_y = self.portrait_center
        self.overlay.move('portrait', cx - previous_x, cy - previous_y)
        self.portrait_center = (cx, cy)
        elapsed = now - self.overlay_ready_at
        appear = min(1, elapsed / 0.35)
        move = min(1, max(0, (elapsed - 0.95) / 0.7))
        eased = move * move * (3 - 2 * move)
        self.overlay.coords(self.overlay_label, cx, cy + 30 - 40 * eased)
        self.overlay.itemconfigure(self.overlay_label, fill=self.blend_color(BG, TEXT, appear))
        self.overlay.coords(self.overlay_subtitle, cx, cy + 30)
        self.overlay.itemconfigure(self.overlay_subtitle, state='normal' if move >= 1 else 'hidden')
        self.animation_progress = eased
        if elapsed >= 2.65:
            reveal = min(1, (elapsed - 2.65) / 0.45)
            reveal = 1 - (1 - reveal) ** 3
            self.overlay.place_configure(y=-round(h * reveal))
        if elapsed >= 3.1:
            self.end_login_overlay()

    def tick(self, now):
        app, c, p = (self.app, self.canvas, self.p)
        text, fraction, phase = app.schedule.presentation(now)
        if app.avatar_busy and phase == 'waiting' and (now >= app.schedule.deadline):
            text, fraction = ('Waiting for profile image lookup...', 0)
        x1, y1, x2, y2 = self.bar_box
        c.coords(self.bar_fill, x1, y1, x1 + (x2 - x1) * fraction, y2)
        self.update_item(self.bar_fill, fill='#554423' if phase == 'backoff' else BLUE)
        self.update_item(self.activity_label, text=text)
        scanning = phase == 'scanning'
        live = phase in ('waiting', 'backoff')
        rx, ry = (self.w / self.base_w, self.h / self.base_h)
        self.refresh_indicator.update('spinner' if scanning else 'dot', now, p(39) * rx, p(136) * ry, live=live, size=p(22) * min(rx, ry), color=YELLOW if live or scanning else MUTED)
        for item, value, font, width in ((self.status_label, app.status.get(), self.font, self.w - p(40)), (self.last_found_label, app.last_found.get(), self.bold, self.w * 0.54), (self.last_label, app.last.get(), self.small, self.w * 0.42), (self.count_label, app.new_count.get(), self.bold, self.w * 0.43)):
            self.update_item(item, text=fit_text(value, font.measure, width))
        busy = app.schedule.testing
        modal = app.login_overlay_active or app.confirming
        allowed = (app.authenticated or app.args.test) and (not modal)
        if not allowed or busy:
            self.close_settings()
        self.update_widget(self.settings_button, state='normal' if allowed and (not busy) else 'disabled')
        self.update_widget(self.interval_entry, state='normal' if allowed else 'disabled')
        self.update_widget(self.apply_button, state='normal' if allowed else 'disabled')
        self.update_widget(self.run_button, text=('Stop' if app.schedule.enabled else 'Start') if app.authenticated else 'Login to Start', bg=RED if app.schedule.enabled else '#286345', state='disabled' if busy or app.login_busy or modal else 'normal')
        self.update_widget(self.login_button, text='Logout' if app.authenticated else 'Login', state='disabled' if app.login_busy or modal else 'normal')
        self.update_widget(self.stop_button, state='disabled' if modal else 'normal')
        self.update_widget(self.pause_button, text='Resume' if app.schedule.paused else 'Pause', state='disabled' if busy or modal or app.schedule.terminal or (not app.schedule.enabled) else 'normal')
        self.update_item(self.account_label, text='Logged in as' if app.authenticated else 'Checking login...' if app.login_busy else 'Not Logged In')
        self.update_item(self.account_name, text=fit_text(app.config['username'], self.bold.measure, p(130)) if app.authenticated else '')
        self.update_item(self.history_caption, text='SYNTHETIC TEST · clears after 10 seconds' if app.ticket_test else '')
        if app.avatar_image is not self.current_avatar:
            self.current_avatar = app.avatar_image
            c.delete(self.avatar)
            if self.current_avatar:
                self.avatar = c.create_image(self.avatar_x, p(32) * ry, image=self.current_avatar)
            else:
                self.avatar = self.text(self.avatar_x, p(32) * ry, app.config['username'][:1].upper(), self.title_font, YELLOW, 'center')
        if self.current_avatar is None:
            c.itemconfigure(self.avatar, text=app.config['username'][:1].upper() if app.authenticated else '')
        c.itemconfigure(self.avatar, state='normal' if app.authenticated else 'hidden')

# Application lifecycle

VERSION = '1.0.0'
ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
DATA = ROOT / 'data'
DEFAULT_CONFIG = {
    'username': '', 'poll_seconds': 5, 'max_backoff_seconds': 900,
    'request_timeout_seconds': 20, 'page_size': 100, 'max_pages': 1000,
    'halo_seconds': 3,
    'halo_width': 28, 'sound_enabled': True, 'toast_enabled': True,
    'halo_enabled': True, 'defer_fullscreen': True,
}
_asset_lock = threading.Lock()


def atomic_write(path, data):
    """Publish a complete local file only after its contents reach disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def asset_bytes(name):
    if name == 'toast.ps1':
        return TOAST_SCRIPT.encode('utf-8')
    return zlib.decompress(base64.b85decode(_EMBEDDED_ASSETS[name]))


def resource_path(name):
    """Cache embedded resources for APIs that require filenames. Never cache keys."""
    expected = asset_bytes(name)
    path = DATA / 'resources' / name
    with _asset_lock:
        if not path.is_file() or path.read_bytes() != expected:
            atomic_write(path, expected)
    return path


def save_settings(**changes):
    path = DATA / 'config.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(DEFAULT_CONFIG)
    data.update(changes)
    atomic_write(path, json.dumps(data, indent=2).encode('utf-8'))


def startup_command():
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        return subprocess.list2cmdline([str(executable), '--background'])
    gui = executable.with_name('pythonw.exe')
    if not gui.is_file():
        raise RuntimeError('The Windows Python GUI launcher (pythonw.exe) is missing.')
    return subprocess.list2cmdline([str(gui), str(Path(__file__).resolve()), '--background'])


def configure_startup(action):
    """Manage only this user's existing E621TicketBot sign-in entry."""
    path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        if action == 'enable':
            winreg.SetValueEx(key, 'E621TicketBot', 0, winreg.REG_SZ, startup_command())
        elif action == 'disable':
            try:
                winreg.DeleteValue(key, 'E621TicketBot')
            except FileNotFoundError:
                pass
        try:
            return bool(winreg.QueryValueEx(key, 'E621TicketBot')[0])
        except FileNotFoundError:
            return False

def format_last_ticket_found(timestamp):
    if timestamp is None:
        return 'No tickets found since launch.'
    return 'Last Ticket Found: ' + timestamp.astimezone().strftime('%d/%m/%y, %H:%M:%S')

def validate_config(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG):
        raise ValueError('Uh oh... The config.json contains unknown or missing settings. Network endpoint and method cannot be configured! ;-;')
    username = config.get('username')
    if not isinstance(username, str) or any((c in username for c in ':\r\n')):
        raise ValueError('config.json needs a valid username.')
    for key, low, high in [('poll_seconds', 5, 3600), ('max_backoff_seconds', 30, 86400), ('request_timeout_seconds', 2, 120), ('page_size', 1, 100), ('max_pages', 1, 10000), ('halo_seconds', 1, 30), ('halo_width', 10, 100)]:
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f'config.json: {key} must be a whole number from {low} to {high}.')
    for key in ('sound_enabled', 'toast_enabled', 'halo_enabled', 'defer_fullscreen'):
        if type(config.get(key)) is not bool:
            raise ValueError(f'config.json: {key} must be true or false.')


def load_config():
    """Use only this installation's settings; never inspect another user folder."""
    path = DATA / 'config.json'
    if path.is_file():
        config = json.loads(path.read_text(encoding='utf-8'))
    else:
        config = dict(DEFAULT_CONFIG)
    validate_config(config)
    if not path.exists():
        atomic_write(path, json.dumps(config, indent=2).encode('utf-8'))
    return config


class App:

    def __init__(self, config, args):
        if Image is None or ImageTk is None:
            raise GraphicsUnavailable('Pillow graphics support is required before starting the bot.')
        self.config, self.args = (config, args)
        self.lock = InstanceLock()
        self.stop_signal = StopSignal()
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.state = None
        self.client = None
        self.session_id = 0
        self.session_stop = threading.Event()
        self.login_busy = False
        self.login_overlay_active = False
        self.account_loading_until = 0
        self.session_counts = {}
        set_app_identity()
        configure_dashboard_dpi()
        self.root = tk.Tk()
        icon = str(resource_path('assets/e621-ticket-bot.ico'))
        self.root.iconbitmap(default=icon)
        self.root.iconbitmap(icon)
        if args.test and getattr(args, 'background', False):
            self.root.withdraw()
        self.root.title("Wisp's e621 Mod Ticket Bot" + (' - TEST MODE' if args.test else ''))
        self.status = tk.StringVar(value='Test mode - no API access.' if args.test else 'Log in to begin.')
        self.last = tk.StringVar(value='No successful scan this session.')
        self.last_found = tk.StringVar(value=format_last_ticket_found(self.state.last_ticket_found if self.state else None))
        self.new_count = tk.StringVar(value='New Tickets Found This Session: 0')
        self.recent = RecentTickets(limit=10)
        self.schedule = PollSchedule(config['poll_seconds'], enabled=False)
        self.authenticated = False
        self.avatar_image = None
        self.avatar_note = ''
        self.avatar_attempt_at = None
        self.avatar_busy = False
        self.ticket_test = None
        self.test_generation = 0
        self.test_requested = False
        self.dashboard = Dashboard(self)
        self.halo = Halo(self.root, config['halo_seconds'], config['halo_width'])
        self.gate = VisualGate()
        self.sound_gate = SoundGate()
        self.sound_duration = 3
        self.test_pending, self.sending, self.retry_visual_at = (0, False, 0)
        self.simulated_until, self.next_gate = (0, 0)
        self.closing = False
        self.confirming = False
        self.worker = None
        self.root.protocol('WM_DELETE_WINDOW', self.request_close)
        self.window_controls = WindowControls(self.root, self.request_close, self.end_session)
        self.root.after_idle(self.window_controls.attach)
        self.root.bind('<Map>', lambda e: self.root.after_idle(self.window_controls.attach) if e.widget == self.root else None)
        signal.signal(signal.SIGINT, lambda *_: self.close())
        signal.signal(signal.SIGTERM, lambda *_: self.close())
        if args.test:
            self.root.after(max(0, int(args.delay * 1000)), self.test_alert)
        else:
            self.root.after_idle(self.startup_login)
        self.root.after(50, self.tick)

    def startup_login(self):
        if not self.config['username']:
            return
        try:
            key = read_key(self.config['username'])
            if key:
                self.begin_login(self.config['username'], key, save=False)
        except OSError:
            self.status.set('Saved credentials could not be read. Please log in.')

    def show_login(self):
        if not self.login_busy:
            self.dashboard.show_login_form()

    def begin_login(self, username, key, save=True):
        if self.login_busy or self.authenticated:
            return False
        if not re.fullmatch('[A-Za-z0-9_~-]{1,100}', username) or not key or key.upper() == 'KEYPLACEHOLDER':
            if self.dashboard.login_form is not None:
                self.dashboard.login_error.configure(text='Enter your e621 username and an API key.')
            else:
                self.status.set('Enter your e621 username and an API key using Login.')
            return False
        self.login_busy = True
        self.login_username_pending = username
        checking_started = time.monotonic()
        self.login_overlay_active = True
        self.dashboard.begin_login_overlay(username)
        self.status.set('Checking login...')
        generation = self.session_id
        config = dict(self.config, username=username)
        stop = self.session_stop

        def work():
            try:
                client = Client(config, key, stop)
                client.page(None)
                result = (True, username, key if save else None, client)
            except MonitorError as exc:
                message = str(exc)
                if message.startswith('HTTP 401') and 'scope' not in message and ('account-level' not in message):
                    message = 'Incorrect Username or API Key'
                elif message.startswith('HTTP 403'):
                    message = 'This account cannot access moderator tickets. Check its permissions and try again.'
                elif message.startswith('Network'):
                    message = 'Unable to reach e621. Please check your connection and try again.'
                result = (False, message)
            except Exception:
                result = (False, 'Login could not be checked. Please try again.')
            if not stop.wait(max(0, 1 - (time.monotonic() - checking_started))):
                self.events.put(('session', (generation, 'login', result)))
        threading.Thread(target=work, daemon=True).start()
        return True

    def complete_login(self, result):
        self.login_busy = False
        if not result[0]:
            self.status.set('Log in to begin.')
            self.dashboard.show_login_form(error=result[1], username=self.login_username_pending)
            return
        _, username, key, client = result
        try:
            path = DATA / 'state.json'
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('username') != username:
                path = DATA / ('state-' + hashlib.sha256(username.encode()).hexdigest()[:20] + '.json')
            state = State(path, username)
            if key is not None:
                save_key(username, key)
            save_settings(username=username)
        except (OSError, ValueError, MonitorError):
            self.dashboard.show_login_form(error='Login verified, but local account storage could not be opened. Please try again.', username=username)
            return
        changed = username != self.config['username']
        self.config['username'] = username
        self.client, self.state = (client, state)
        self.authenticated = True
        self.account_loading_until = time.monotonic() + 0.3
        self.schedule = PollSchedule(self.config['poll_seconds'], enabled=False)
        self.recent.rows = list(state.history)
        self.last_found.set(format_last_ticket_found(state.last_ticket_found))
        self.new_count.set(f'New Tickets Found This Session: {self.session_counts.get(username, 0)}')
        self.dashboard.refresh_tickets()
        self.status.set('Ready · press Start to monitor.')
        if changed:
            self.avatar_image = None
            self.avatar_attempt_at = None
        self.maybe_avatar()

    def logout(self):
        try:
            delete_key(self.config['username'])
        except OSError:
            messagebox.showerror('Logout', 'Could not remove the saved login. Please try again.', parent=self.root)
            return
        self.dashboard.end_login_overlay()
        self.login_overlay_active = False
        self.session_stop.set()
        self.session_id += 1
        self.session_stop = threading.Event()
        self.test_generation += 1
        self.ticket_test = None
        self.test_requested = False
        self.test_pending = 0
        self.halo.hide()
        self.authenticated = False
        self.login_busy = False
        self.client = self.state = None
        self.avatar_busy = False
        self.avatar_image = None
        self.avatar_attempt_at = None
        self.avatar_note = ''
        self.schedule = PollSchedule(self.config['poll_seconds'], enabled=False)
        self.recent.rows = []
        self.last_found.set('Log in to view saved tickets.')
        self.last.set('')
        self.new_count.set('')
        self.status.set('Logged out. Log in to begin.')
        self.dashboard.refresh_tickets()

    def toggle_running(self):
        if self.login_overlay_active:
            return
        if not self.authenticated:
            self.show_login()
            return
        if self.schedule.testing or self.schedule.terminal:
            return
        self.schedule.enabled = not self.schedule.enabled
        self.schedule.paused = False
        self.schedule.present_until = 0
        if self.schedule.enabled:
            self.schedule.deadline = max(time.monotonic(), self.schedule.not_before)
            self.schedule.wait_started = time.monotonic()
            self.status.set('Monitoring started.')
        else:
            self.status.set('Stopped · press Start to monitor.')
            self.halo.hide()

    def play_sound(self):
        if self.config['sound_enabled'] and self.sound_gate.ready(time.monotonic(), self.sound_duration):
            try:
                sound()
            except Exception:
                self.status.set('Sound could not play. Check WAV file / Windows sound.')

    def test_alert(self):
        if not (self.authenticated or self.args.test):
            return
        if self.ticket_test or self.test_requested:
            return
        self.test_pending += 1
        self.play_sound()
        if self.args.simulate_fullscreen:
            self.simulated_until = time.monotonic() + self.args.simulate_fullscreen
        self.status.set('Alert Test: sound requested; visuals wait if fullscreen is detected.')

    def toggle_pause(self):
        self.schedule.toggle_pause(time.monotonic())

    def set_alert_option(self, key, enabled):
        if key not in ('sound_enabled', 'halo_enabled') or type(enabled) is not bool or (not (self.authenticated or self.args.test)):
            return False
        try:
            duration = self.sound_duration
            save_settings(**{key: enabled})
        except (OSError, ValueError, wave.Error):
            messagebox.showerror('Alert settings', 'Could not save that setting. Check that the application folder is writable.', parent=self.root)
            return False
        self.config[key] = enabled
        self.sound_duration = duration
        if not enabled:
            if key == 'halo_enabled':
                self.halo.hide()
            if key == 'sound_enabled':
                stop_sound()
        return True

    def change_interval(self, raw):
        if not (self.authenticated or self.args.test):
            return False
        try:
            seconds = int(raw)
            if str(seconds) != str(raw).strip() or not 5 <= seconds <= 3600:
                raise ValueError()
        except (ValueError, TypeError):
            self.dashboard.show_interval_error('Choose a whole number from 5 to 3600 seconds.')
            return False
        try:
            save_settings(poll_seconds=seconds)
            self.config['poll_seconds'] = seconds
            self.schedule.set_interval(seconds, time.monotonic())
            self.dashboard.show_interval_error('')
            return True
        except (ValueError, TypeError, OSError):
            self.dashboard.show_interval_error('Could not save the refresh interval. Try again.')
            return False

    def request_ticket_test(self):
        if not (self.authenticated or self.args.test):
            return
        if not self.schedule.start_test():
            return
        self.test_requested = True

    def begin_ticket_test(self, now):
        self.test_requested = False
        self.test_generation += 1
        self.ticket_test = {'until': now + 10, 'rows': list(self.recent.rows), 'status': self.status.get(), 'last': self.last.get(), 'found': self.last_found.get(), 'count': self.new_count.get(), 'visual_retry': self.retry_visual_at}
        self.recent.rows = [('TEST-621', 'Synthetic ticket — this is a local test, not an e621 report.', datetime.now(timezone.utc))]
        self.status.set('Ticket Test: synthetic data clears in 10 seconds. Live refresh is paused.')
        self.last_found.set('Last Ticket Found: TEST ONLY')
        self.new_count.set('Synthetic Ticket Test: 1 (not counted)')
        self.dashboard.refresh_tickets()
        self.test_pending = 1
        self.play_sound()

    def finish_ticket_test(self, now):
        saved = self.ticket_test
        self.ticket_test = None
        self.test_pending = 0
        self.test_generation += 1
        self.recent.rows = saved['rows']
        self.status.set(saved['status'])
        self.last.set(saved['last'])
        self.last_found.set(saved['found'])
        self.new_count.set(saved['count'])
        self.retry_visual_at = saved['visual_retry']
        self.halo.hide()
        self.dashboard.refresh_tickets()
        self.schedule.end_test(now)

    def start_scan(self, now):
        if not self.schedule.begin(now):
            return
        generation, client = (self.session_id, self.client)

        def work():
            try:
                self.events.put(('session', (generation, 'scan_result', client.snapshot())))
            except MonitorError as exc:
                self.events.put(('session', (generation, 'scan_error', exc)))
            except Exception:
                self.events.put(('session', (generation, 'scan_error', MonitorError('Unexpected polling failure; log in again.', retryable=False))))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def maybe_avatar(self):
        now = time.monotonic()
        if not self.authenticated or self.args.test or self.avatar_busy or (self.avatar_attempt_at is not None and now - self.avatar_attempt_at < 900):
            return
        self.avatar_attempt_at = now
        self.avatar_busy = True
        generation, username, stop = (self.session_id, self.config['username'], self.session_stop)

        def work():
            result = fetch_avatar(username, stop)
            self.events.put(('session', (generation, 'avatar_profile', (username, result))))
        threading.Thread(target=work, daemon=True).start()

    def blocked(self):
        return time.monotonic() < self.simulated_until or (self.config['defer_fullscreen'] and fullscreen())

    def deliver(self, real_count, test_count):
        self.sending = True
        generation = self.test_generation
        try:
            if self.config['halo_enabled']:
                self.halo.show()
        except Exception:
            self.status.set('Halo is borked. Check desktop session and run test alert.')

        def work():
            try:
                if self.blocked() or generation != self.test_generation:
                    self.events.put(('visual_deferred', (real_count, test_count, generation)))
                    return
                if self.config['toast_enabled']:
                    toast(real_count + test_count, test=real_count == 0)
                self.events.put(('visual_done', (real_count, test_count, generation)))
            except Exception:
                self.events.put(('visual_failed', (real_count, test_count, generation)))
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        if self.closing:
            return
        if self.stop_signal.is_set():
            self.close()
            return
        try:
            while True:
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'session':
                    generation, kind, value = value
                    if generation != self.session_id:
                        continue
                if kind == 'login':
                    self.complete_login(value)
                    continue
                if kind == 'avatar_profile':
                    username, result = value
                    self.avatar_busy = False
                    if username != self.config['username']:
                        self.avatar_attempt_at = None
                        self.maybe_avatar()
                        continue
                    kind, value = ('avatar', result)
                if kind == 'scan_result':
                    self.schedule.failures = 0
                    self.schedule.finish(time.monotonic())
                    self.authenticated = True
                    self.maybe_avatar()
                    kind = 'snapshot'
                elif kind == 'scan_error':
                    self.schedule.failures += 1
                    retry = None
                    if value.retryable:
                        base = min(self.config['max_backoff_seconds'], 60 * 2 ** min(self.schedule.failures - 1, 10))
                        retry = max(value.retry_after, min(self.config['max_backoff_seconds'], base * random.uniform(0.9, 1.1)))
                    self.schedule.finish(time.monotonic(), retry_delay=retry, terminal=not value.retryable)
                    if not value.retryable:
                        self.authenticated = False
                        self.schedule.enabled = False
                        self.client = None
                    self.status.set(str(value))
                    continue
                if kind == 'snapshot':
                    baseline = not self.state.initialized
                    fresh = self.state.accept(set(value), value if isinstance(value, dict) else None)
                    self.last_found.set(format_last_ticket_found(self.state.last_ticket_found))
                    if fresh:
                        self.recent.rows = list(self.state.history)
                        self.dashboard.refresh_tickets()
                        if self.schedule.enabled or self.args.test:
                            self.play_sound()
                    self.status.set(f"Baseline saved: {len(value)} {('ticket' if len(value) == 1 else 'tickets')} in queue." if baseline else queue_message(len(value)))
                    name = self.config['username']
                    self.session_counts[name] = self.session_counts.get(name, 0) + len(fresh)
                    self.new_count.set(f'New Tickets Found This Session: {self.session_counts[name]}')
                    self.last.set('Last Successful Scan: ' + time.strftime('%H:%M:%S'))
                elif kind == 'status':
                    self.status.set(value)
                elif kind == 'visual_done':
                    real_count, test_count, generation = value
                    if self.state and real_count and (generation == self.test_generation):
                        self.state.acknowledge_visuals(real_count)
                    if generation == self.test_generation:
                        self.test_pending = max(0, self.test_pending - test_count)
                    self.sending = False
                elif kind == 'visual_deferred':
                    self.sending = False
                    if value and value[2] != self.test_generation:
                        continue
                    self.halo.hide()
                    self.gate.safe_checks = 0
                elif kind == 'visual_failed':
                    self.sending = False
                    if value and value[2] != self.test_generation:
                        continue
                    self.retry_visual_at = time.monotonic() + 60
                    self.status.set('Toast submission failed. Visual alerts remain queued; retrying in 60 seconds.')
                elif kind == 'avatar':
                    data, self.avatar_note = value
                    self.avatar_busy = False
                    self.schedule.deadline = max(self.schedule.deadline, time.monotonic() + 1)
                    try:
                        self.avatar_image = tk.PhotoImage(data=data) if data else None
                    except tk.TclError:
                        self.avatar_note = 'Avatar could not be displayed; initials shown.'
                        self.avatar_image = None
            blocked = self.blocked()
            if blocked:
                self.halo.hide()
            else:
                self.halo.tick()
            now = time.monotonic()
            if self.test_requested and (not self.schedule.inflight) and (not self.sending) and (now >= self.sound_gate.available_at):
                self.begin_ticket_test(now)
            if self.ticket_test and now >= self.ticket_test['until']:
                self.finish_ticket_test(now)
            real_pending = self.state.pending if self.state else 0
            count = real_pending + self.test_pending
            self.dashboard.tick(time.monotonic())
            if time.monotonic() >= self.next_gate:
                self.next_gate = time.monotonic() + 0.5
                alerts_enabled = self.authenticated and self.schedule.enabled or self.args.test or (self.authenticated and self.test_pending)
                if alerts_enabled and self.gate.ready(blocked, count) and (not self.sending) and (time.monotonic() >= self.retry_visual_at):
                    real = self.state.pending if self.state else 0
                    if self.ticket_test:
                        self.deliver(0, self.test_pending) if self.test_pending else None
                    elif real:
                        self.deliver(real, 0)
                    elif self.test_pending:
                        self.deliver(0, self.test_pending)
            if not self.avatar_busy and self.schedule.ready(time.monotonic()):
                self.start_scan(time.monotonic())
        except OSError:
            self.status.set('Local storage or Windows integration failed. Monitoring stopped; fix the problem and restart.')
            self.stop.set()
            self.schedule.terminal = True
            self.schedule.inflight = False
            self.dashboard.tick(time.monotonic())
            return
        self.root.after(50, self.tick)

    def request_close(self):
        if self.closing or self.confirming:
            return
        self.confirming = True
        try:
            self.dashboard.show_close_confirmation(self.finish_close_confirmation)
        except Exception:
            self.confirming = False
            raise

    def finish_close_confirmation(self, accepted):
        if not self.confirming:
            return
        self.confirming = False
        if accepted:
            self.close()

    def end_session(self):
        self.stop.set()
        self.session_stop.set()
        self.closing = True
        self.halo.hide()
        self.window_controls.detach()
        self.root.destroy()

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop.set()
        self.session_stop.set()
        self.halo.hide()
        self.status.set('Stopping...')
        self.root.after(50, self.finish_close)

    def finish_close(self):
        if self.worker and self.worker.is_alive():
            self.root.after(100, self.finish_close)
            return
        self.window_controls.detach()
        self.root.destroy()
        self.stop_signal.close()
        self.lock.close()

def graphics_install_command():
    """Target the interpreter that opened this file, not another Python on PATH."""
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    quoted = str(executable).replace("'", "''")
    return f"& '{quoted}' -m pip install --upgrade --only-binary=:all: --index-url https://pypi.org/simple Pillow"


class GraphicsSetup:
    """The startup screen when Pillow is missing; no account or monitor is loaded."""

    def __init__(self):
        configure_dashboard_dpi()
        self.root = tk.Tk()
        self.root.title("Wisp's e621 Mod Ticket Bot")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.ready = False
        icon = str(resource_path('assets/e621-ticket-bot.ico'))
        self.root.iconbitmap(icon)
        body = tk.Frame(self.root, bg=BG, padx=26, pady=24)
        body.pack(fill='both', expand=True)
        tk.Label(body, text='Graphics setup needed', bg=BG, fg=TEXT,
                 font=('Segoe UI', 17, 'bold'), anchor='w').pack(fill='x')
        tk.Label(body, text='This Python installation needs Pillow for profile photos, smooth alerts and the refresh indicator.',
                 bg=BG, fg=TEXT, justify='left', wraplength=610,
                 font=('Segoe UI', 10)).pack(fill='x', pady=(12, 8))
        tk.Label(body, text='Run this command in PowerShell, then choose Check Again.',
                 bg=BG, fg=MUTED, anchor='w', font=('Segoe UI', 10)).pack(fill='x')
        self.command = graphics_install_command()
        self.command_box = tk.Text(body, height=4, width=78, wrap='word', bg=CARD, fg=TEXT,
                                   relief='flat', font=('Consolas', 9), padx=10, pady=8)
        self.command_box.insert('1.0', self.command)
        self.command_box.configure(state='disabled')
        self.command_box.pack(fill='x', pady=12)
        self.feedback = tk.Label(body, text='', bg=BG, fg='#FFB4B4', anchor='w', font=('Segoe UI', 10))
        self.feedback.pack(fill='x', pady=(0, 10))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill='x')
        style = dict(bg=CARD, fg=TEXT, activebackground=BLUE, activeforeground=TEXT,
                     relief='flat', borderwidth=0, padx=14, pady=8,
                     font=('Segoe UI', 10, 'bold'), cursor='hand2')
        self.copy_button = tk.Button(buttons, text='Copy Setup Command', command=self.copy_command, **style)
        self.copy_button.pack(side='left')
        self.retry_button = tk.Button(buttons, text='Check Again', command=self.retry, **style)
        self.retry_button.pack(side='left', padx=10)
        self.close_button = tk.Button(buttons, text='Close Bot', command=self.root.destroy, **style)
        self.close_button.pack(side='right')
        self.root.bind('<Escape>', lambda _: self.root.destroy())
        self.root.protocol('WM_DELETE_WINDOW', self.root.destroy)

    def copy_command(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.command)
        self.feedback.configure(text='Command copied. Run it in PowerShell, then check again.', fg=MUTED)

    def retry(self):
        if load_graphics():
            self.ready = True
            self.root.destroy()
        else:
            self.feedback.configure(text='Pillow is still unavailable in this Python installation.', fg='#FFB4B4')

    def run(self):
        self.root.mainloop()
        return self.ready


def ensure_graphics_ready():
    return (Image is not None and ImageTk is not None) or GraphicsSetup().run()


def report_error(message, args):
    if (getattr(args, 'gui_errors', False) or sys.stdout is None) and (not getattr(args, 'background', False)):
        error_root = tk.Tk()
        error_root.withdraw()
        try:
            messagebox.showerror("Wisp's e621 Mod Ticket Bot", message, parent=error_root)
        finally:
            error_root.destroy()
    else:
        print(message)

def main():
    parser = argparse.ArgumentParser(description='E621 ticket queue notifications for Windows')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--test', action='store_true')
    group.add_argument('--stop', action='store_true', help='Gracefully stop the running monitor, including hidden startup mode')
    group.add_argument('--startup', choices=('enable', 'disable', 'status'), help='Manage current-user sign-in startup')
    parser.add_argument('--background', action='store_true', help='Sign-in launch: show the stopped dashboard, suppress startup error dialogs')
    parser.add_argument('--gui-errors', action='store_true', help='Show startup errors in a dialog for console-free manual launches')
    parser.add_argument('--delay', type=float, default=1, help='Seconds before the initial test alert')
    parser.add_argument('--simulate-fullscreen', type=float, default=0, help='Defer test visuals for this many seconds')
    args = parser.parse_args()
    try:
        if args.stop:
            print('Stop requested; an in-flight request may need time to finish.' if request_stop() else 'No running monitor found.')
            return 0
        if args.startup:
            print('Sign-in startup enabled.' if configure_startup(args.startup) else 'Sign-in startup disabled.')
            return 0
        if not ensure_graphics_ready():
            return 1
        config = load_config()
        app = App(config, args)
        app.root.mainloop()
        return 0
    except (MonitorError, ValueError, RuntimeError) as exc:
        report_error(str(exc), args)
        return 1
    except (OSError, tk.TclError, wave.Error):
        report_error('Could not access a required file or Windows desktop service. Check README and try again.', args)
        return 1
    except KeyboardInterrupt:
        return 0

# Embedded binary resources (zlib + Base85). No account data is included.
_EMBEDDED_ASSETS = {
    'assets/e621-ticket-bot.ico': (
        'c-oYEb8Ii~*6+8rZQJdxZM)sIZS2~%ZQHhO+qS!F-QPLqy~+E>O-}BUHJSC9XOfvome(2p00;mDKtu%mvj_pvzyLtXKO8FRf3PJ8'
        '06_B3fr<GaTm}jNM1%js5&j1+|HHGw005Mf|H0l+001;H0KmxjADjgX0EFTI00IL4!TkU1k^FP7pWlD*6BPhp$OZVHMF^PD1OU{G'
        '0f2A?IdM2>Z0LV3!bwVqDE)K(*Bc^2{_{Kl!Q+ns0068aDI%!imQ|dnC9Nt52<}bi{kZzmh0A*EG+#wY6Gk6ajJN<rOH4{h8rbKH'
        '$O|b68P4$K=Yd?1SCC40{)}GaFM>dX7LVW}e?^+n+W04z#gp9SfNXc-sVU8%+5p|EpPC3gtXSHyOrBn+*uTfb3u^&$e%t0%`ZKmp'
        '=Mftzer&o#o`5umBan$GCV`v)#1$2KB3=-T5=aQEODGicWoUY)H2$|Jy^r!A)<1#>JzK#ogse(Kdk08v-`MzI3Ks$O`h}s#Oq)K('
        'lu^FuE%!$r5B(7Khi~m(a&8V-;6%5RD+!gAg-55DT05}usmK(%0rQ-dQkB}!C?ORE*9p#+xMZ94BbeuHmZnnK47=5u&-iMqN|`Ew'
        'dc`?KmPwoLQo+?m5uAsFQDpntd5mlYkiZB=@O}w7<Lf{PTNPX5*Nx3GQ=rz{dV}rKg<TiEkzW<@V&d~qpLIDF-ogi-^QBT(x#n=G'
        '>{r6lsl-C2rXcSSAz0M%_xg;HFNO(>`6rEWEop5|q;CWoL1$?C#D^<{!nO;ghckpEhZ^rYCKBm>9^8@TWdg*7&*}%d44F?wLv)hq'
        'bK2sN>4Wn&->G%;a%UMEcpGXCylB#AG3T)!hgWUz(lpJeL4;t+J_rn_6=){9V%X1!VITDeM{EH+v0nz{3B8Og8k$#-p*H9VRNAyv'
        '<>1*rDU)7yF`Vi0(xR3e^Fg%MEF+dwMZy}$jzl8q>zEw)kQ7673a==x!;O^)7#nTslRp&?2mKRF#f#Efn&HVtns7cK4(Ong^61Z+'
        '{}M+@Q8|$sA^m{=hcNz2Jy8EGjM3qEZ~y=Z(|?3<I!VtQR~2={_jHT9F>~8kvV%z~6PYfhWq!psZ-fxLP*ACet_^`$J^pz-;A9>;'
        '1XXX7u;5G>DayCc&wMywE%ZV-;#V{}G@mhDGrGlwz(yKdXL|d~gmuL)NNCYHnAH85tdHrJ?8j`7pBJ)K0${Yp11m7h-sey5xO<0}'
        ')Kq_iZpGjG9i4C?`ASUddO~_8{ED_HK(UyjbW31*ZDHt}2Kk_Dod$x@;znd<@{^;r*W&`gNDKyU>L$iR6yBQ<)&+X30fByEgwTpu'
        'x_V%{-+)|aK`pTn+R-&r%3p%www+z=r;p(EfCEp6-35KuLqZK1q6T8f=LM6q+h7U;{|m|2Gf-zeabYB?7UU-Mf)+W-CQ8vqzT57<'
        '0cemjlkYO2(#WOptd_8nNvFq~Y%DL*{wJ*zX|bAa$0_t|&Zu|U;4FX8*>(Ds={*w}sOYVy67tiH|6EN{#Muv}jgf{Wrei%%%68R1'
        '<F8!#L^#*-jjGJ*T>UjEs>ajp%5@ri2?}0HZ_7LSbY*5+3Jm-8aO%;DPk1MAksa$@(aYA#l%+Mg<F9poNgY3<docIt%CbH`4uQgN'
        'xRYM=Y=CnV`0g~WY$WaG@>fR)a`G5HANI)WH%#tC59>?9kyP4}BJGJrm8TdVZ}7^+)%C`}KzySJ3}0m~B6Dq+=s4qC6|?EVom`X`'
        'id7L|%}1*SrMg%z$8i23|Ki5R(EWMj>(C!@?;LGhiQ&M}^6+GithoC8QkHQzf{sw|QMAtKWhFSnD9z)Z81eEHwZ-22sEYl&Bdjb;'
        'xg35mS7S*su|=`~v7IW-%kj`!ES!xFS`7)L@Tg`Y$t>m5t)4&1wvX?b+k1mJIxp9FGgY>(ou-vF9}{3|T3r{U<(z7IP75jzq~TAG'
        'Y75{?1S9aV*JW{M6t6PP1Is}$AhC&;w+069xPO#YZ>qjQ9h$7KXvtTB$wS&7JCDxJ`mNDvTXj=breYCnxyy)lultcinpHq?gn5DL'
        '<BV;)!PA;^!r`Q!SRDWMvE<KlJ=np8))`t>1E$x$+l60GdA$xrzB8Y=OC9twPg!2w|BI&m%kg0TThsPjd0znlP`m$V+I6x;*l$(L'
        'QQz72$<C=MZY!3lHdQeuG3=)Bvnek|b4xK<NYp~}#$jaHa6S`i5rbf4G#OCjjZ|yJDOB04c|uTWR7*#L25roA*Q_URvmMV_PKVnx'
        ')3^RU20?_a$^ER*ANQ1>NB+CsFPuigRUF~(eIn*((HdXPCKbOJAMm8^gIDO@UJWmQ8*3*EJLavQK~g6V8p_}>LijM?V<Ex3DuM))'
        'xlCYoXc7N5BgO()(P0JU7QZKHA47*s8k<>gHRC*-6Q45c1I{P|*e#&Z$iNYX&}MfId)S^19A@kx0%^JjWj@k~yd~R|ST!CGCL)K!'
        '4A!Yk+o2j2pvU<_14d*@oYY~#s$rao{)Y9q9B7mm#Am#|vtL0(2W0gt#1n&5cxGtBkyneiCX@#{E4^#@WR}fPsg&%z1iadFV3(1|'
        'TL;9Tq2FEtFGCJT8{AA<@Jfy!HcQKLglMR9!n|(j)b<hc!T8oS7+c2v@dv-J^o2y8!=oLuSk1^sdII842-OfN;*;k4am$P3FNerm'
        'G97Q)IfbdS^A7K@PRa~gQjDQ6n)lGJ`@K3bxNe5W@*HR=ghw`|(|_1ZwmZ?HqotnhF8i45_BoJbt<8y8d4@1-(Kd@CQcq>~n@39?'
        '7q-f~?x$c|+R?5s5x1U6&MaDybI$wG1Ip&nca`(iUIo~?>LR@<$*!*r*5*0_6l_RfordsNEgi#2X=rW<mE#T9g`Ym%8bWh@FOQc~'
        'Jji#Ih7+_y@)}WA@F>>e{hdT~6r-ZdTX#Q(_?iu>n`+?;!yKxpn23wj>orA^;MLV!DRjt|-TEmHC?_Jk-b0{y=AU^nIzC>T8$w)V'
        'SZ8XC(h@+`Q4(3nV<niRq)4pMrv0vd-kl4bSaL26jg$^Th&Y5=*he>JW0~_S!myrLonE5nGVx+&w<x=k0^tHNS2ZgoNp++GgCFJ>'
        'id!6#FBg`Kt18mO@-XzH9IA%hpnm`T8aR2#Jfp2BvbjX+m_f*|E<U`oP?5}_BJrl3#2+M$xMyJ}v;{_ihcPf8>qOW*-Q|~jdK13S'
        'J5Q&}Ov=<1;VUS=7j}>=@^&G&w5aKdJ)VA2Ni99c!Zn$6rQlfR9lH5+FxBG-uIWG$cR%Z=o%iE1IfOB`^xFE)Zg6n_!(*OLk~tR~'
        'KH*@-*)z{YP!TU^L~zF6|2f35pt|TVPibYpAc>EAX6*6<^#HzgWg)YrC;l06uSnvR`1O_dFu)d>Yz|Ls5osdDKJ}%d;k$2Au7G)r'
        'Lhp;r6vv-w^4A<AIa{YJ3E@CziHHK5@or&m_Yux>#~N*}UPWZ@xn?*A*AfPJ-U=CvB@xjCmu7x>0{j)HH|PqGa~gQ)2hZd#DCx$D'
        '2o>chMuvH&btt|UTlH|<xEwE{tF*y^!hL>E8Z08C$1$mKv8RnQ2b^_?tDvPNt<w~Cy#RdHGI0K>SLtYj5Tot<lA(R<(QE1WRC>m5'
        '?F#^E-!w=x`2F}_?9{&kV#0shsXrbbe*pmStpBl7Dc<JGt5~D_r&+tVJ-tIiL*$AyE3Ihh@$Clr%eJr|t4_heOedHD&eqkN>R={y'
        '#hNQIEwC#K3(e0qjj+@vXKWKiLW>%!EA1B1F3#1OR#9TNvbK(PH(%Y^EHk%ai2>iO{Ucs`TZ3N?Gml=mt+#*nLOy#V+g8MdbBk{I'
        '^{qc#rhT13VIhG={`!<=T9+g<GOu>0gYably)u@f+wB4OANhy^+b;m0M}?pIKHob7Va8SI(zQRzO=phs36Nr779t6;#;)tg+$aUq'
        '=6M1e1BW#1X;wW)WogRXEckmlrajS|G&U~mtsI_6ab;OZ{QCOZC|jvTssq%P1RqKi<*1k+k_bZG!8Jinm7v`92wZ8xF=wc-LoxOo'
        '{u2D)B2K^1-yOG$*v`ytcL80gofX3%UkMlcn*=M1Up|+gvJsmBjf7b|ClQ2Ekh32178SzJAFzW&WKvN+jUAo1-bIq=Zrn1Q7i=Kb'
        'BB*xqLF=f8gu@=A)%lEiQq#%(ls;crf!{;>Rc78R4yOlx=p0AvvJ((@sG4wiNXpj=%BnlO{>(Kq03q21KaztKQ1@GiveZJ4YG9tI'
        'U>*jTjI8hp)~q|bONW5bn}If6%AWwONTGG^u3FHhTGTn$m<b|W1f?_y7CUru*kwza=sQM-QmtR!C7yQ1B>uS%W*#F%k&e^;VUI&0'
        'E!g*)w1Q)DB_Ll3rHK2qAlqe+fXPphjvig)FL8`EMYMT8W*{m$t8RI~9QgLih|V812kn#m-=Q@!)^SX^#ze#Z3l6`XPWN&MghFFE'
        'A1UX#MVHQxAy-Ak^W&MRD7g-eFMJ6f<aDA1q<?ZE{NVa4nypMoU(N`d(2D24Yi|_qE_v8WLl5O(+|llbRu~#fN+YGF%`R(yCU$zW'
        'Or%}d>=*;8R`qwJh8EcV&LhM<6+2)vkrQXTn6th_Qrvn!OI?q@wzjHeu<GR=Vm5A&H@?lcfECSWNus&8)d83k%}%>r>&jSWJ9_x<'
        'UkP?ki>F&3--d1eP7>F`LU17P$SzSLU9$$&q9bX5tl()8`bVobO<2Dj)0NaRxaf|gh+S-VXyOqv@)JW(8$TKa^+f}>xH6Q=Bs|B^'
        'T6u82y(%TcS)n^@4x~37DOtZR2#IW;js~|!xi*vNaR>agRZ~pD$kL<yHro?it}+V)N^5n3_LHEY66trDMGHqPqtyS2%3NQr2Ss`d'
        'N|fQNOfwva<Zw60^335IgR#pIDk38ohebd;A$jhJTq%$Y%RNw9{Jia;0*+4uR^W4vN%{aoerS~-Yhv2)h>dY8oRcim))rUjQ8`|A'
        '<;$;*GyU0}HE@^08O$Hd-6O><n!lQKEqF{HZ<1%~X3{()&nMI?RNd|0JAWww;PT-{eD!jpQr~A?>v-y7MJv+3z~LR1Xr?Q0lqDT_'
        '_HvC~-e#ZXN7J-dmPRTS`%jP^jMVG9XyXc4%A$(q4W8dvZGj$Trw%jRa1-io?6ge;nL69S?{CMJ_68$8GGX>!NG>Z0L`U6OI}l}_'
        '?8HFx5_9JQPfwUMx_=BIrf_F@B_Ix~bxmIJexP^!VNyKD-KdPu>Q2&s{bhMFEk^DbP=s4V_JyMf6C0lE+qpzaOmQ}Vf-oeBo?>&j'
        'Hj;Ceer^4Wga;$ZUF~$O;kmJ?^|-A(&2YI|=jvk<3!D#^Jo`7#1%eSSyUXNt+A6NlhJFEnDmjdVdEY5<(pFfs7XqG5IZs9>vo65u'
        '9?}ABtVmj+97*mMBMK{iyBlaBuI2_z5)Kl9ZjItI$-*#fzQejU=k&n@A&M?Yg-JpEzS|qdceo6O09i($q@<HYnVfqSm*{Zo6YPZW'
        'Xo}+Mu0TT|;Mh2U*ik6|%TGb-_cHxM;DqE!Ia*m(SZNOzyA*X<!XtfswF^3)SX(;qM&;;8)OFDntV++eeiKuCN`a<_dW>3*+3ec6'
        'yzF4*^80YDPpSf+C?XhTamJ%wez6c6&ss<M_<b=GrP`hs)(#i+FxKJ@h?+|#{3d*h$QEdEKE$v;g^$Oz!`5acCO$f?F74mGyS!iH'
        '&nN2HDyEWTzZmV<WZdk(nWr+a<awx3@(%vWvhdlg->p{mzpk@vza4j*gyf<Y^zwJzBu-tnduj|Ao92T=*W>Y0tvp7ONfhT5vc?1~'
        '9~?DkYgtQM1v-+&^ApE(xrO~?o4MaY-P^IZc)IeW*p9&&WPHLH|6Rh4LrgmPFHY@WRUOKIJGH3qHX8r{O5}f>TAF*5jw;?iPHoF`'
        'm+O&B+Kg+FYTG8?8V)T^luNTXh(1X`RCF%S?@nCMSQ<4%DPdzQ0a7YL1UOm@myr}vP)JxjLSI@kSQFZuyLocTi7WfO%YD=PCCg~0'
        '`?h(JD(#GbJmEVb^<#&3^W}5p_O>62lZfKzM{B4QE-eOr9Q^19RFqq@O1u0`#(WOZa=PwJt5iw3(@y{^io+#`MYEpSqpdIB?Jo-o'
        'E;pNt?dvj7AzXM<9{Ce`>~?c{%*T+r?1oKj^4nwa*#)!;U#nEgG4sT+IZJ2zFxAmK!UN@=66^`A|MvGGc$gZH>ME!BYUpiNM1&IJ'
        'qAyYyLmC|3^Dz@3)_i1+D7|qTZIk<Ji!4=M>Ui2S$w`(&R07F{>m(3VO=v#Y0^}@{UC<ZjVg3&hkM9x>C%8W7f+j=;3lPh~X@sYp'
        '*RIniJ-9DSMF!8z5J@V%og~J@tl|N1(h5h-(19{>gA-U5WHYr84HQzKnQid9Y+5t|(MnF>xM|<AK5me5y@1m?WLaiZj+Dg#m87&K'
        'PU#XI8cof%a};>&>sA2GNkBU^e;YO#ITB^`p+sRn!HiMV_6|Pc_Bec`8!SuOu$DK@%t3IO8rwdt^Z|oL@EQr?%9L1MAoD`B>dFQ$'
        '@wVIaV%)fc3yUx_UD=-xXC2FI)b|%2x5eD+{2QcPn9ohJdOIcR&0*|w2^^;4exxlYWQz1MKoaITBDXz2F)<`$WX!9T*RQb@L#{j2'
        '_)<e*3t`+CvS{f?-VmjPs0gPV6eF3S|Mo5HRnm;GD=1H80T(jEnA01F^<5hX1KS~2M-i!7M`it@AqdgIQ%kjp-PPVIA^uVd!pH!7'
        '`q`K_G*Py#CiHVSGR+MasaCw!4Ki7Ri6XsVCE%VF)|4nG&=_P-aB6R<wcv5EY~N?Aq+CB~=!}^}G*;vcC7IIJwnc3~;w=RI0k-^j'
        'e3^rJrDo-a5wsoHXNO`vE}va@Qj}%JtWOrva;ve&wA8}<Z{Jf}#oDZ0D8Ep1^MQ67s>8;3+!w1y8P1m}jP1Qj(5a|Zl}VGo%9c^r'
        '2b062CS;zGOkCRWq)|uft8)1fbklD^CVQv6@a1KtGps!IyK=j-{SXU``^hEau~Uxn;+$JON9IN?tMv`uvBTOceSJ!Za?skBZt;(G'
        'Wk}mMY+GfLlRKEO9JE*Y*8T1;)uby36Z=e4O5D}_c~6(0g!9#AI-QK4ped+=#>G^nE~MgsIgOYw<b)`NE<CHf^1sF+J$)976n8s='
        'sSYm<oKw~{2FvUx56|-GEbW{i!_2O~WW;U0<MtxejBb3u<1K~kqziQ=1t&x_YfU*<o6UFd;OIdy!ArC2_1CNRXw<Z8Cq3W<*L!%;'
        'dDu)OZ!Ed8^BP_@GG_`qeS?-%dkt=$vSoVq-7OR7t$O~jy_)PeSiP#2BnK&#6`vYB-xf7(2qSRr#Q;;<Qy#$uerKPIjQZ-^x?9%_'
        'n?xb8Mc{RG0OOSraEj?l=T$`T0xQ4>a#7aXe#(3=F@nX<&hrorVkM5=?|&O`+4Y8nA1=BHF%qE|&2NPlT_~YO0KJT4w9{$`FE;uL'
        'z`+~5Gly*u%J7e(!-53oA&K+AHb@J6q*1c5#jcIb6*gmtRj<IhQE`z^3!9^nTbmF<!qOcg!<u?~fWutzCY=V;d7cx{E4H{RnR|!^'
        'EZdYmjy>BtwS!u?$}r*JfhyJr#BpE`>s(`ZC@grZa2eLRWM3AcJ0sZ%P!b_dnCUFix-3VH(qYWy4EFaJu-$SEO+jJRysc;cbwOY}'
        'onkZ0F}_^sW~BE=L@uBF?ArgcrMNaeWX<ox%7lwB6XzoR>rMsn^my)RM9hKXR$97tG*v#&2@!R&1{(HWCc!3@6=O<sDC{!$yUcsI'
        'X@zU0R(oveMDzW<QO3N&<PgGJ+v|2RDX3m9C;>m!^yNMP-_H=<_vHg}KQ7CgkJ@uib4tuXfcEAB{LQcNdByXaE>lr6Cbq%&h?EF('
        '^swm)-*&q-`J+_NPnlj|%fxIJ(QNyTSS^P=d0e*n>I$di&!HUj20C%465=Y5l}GDAv|XX~Q$BDM+;n}%JT|Z*KYDE4)H-wM!)LLY'
        ')VVfI<hKwi6YeNza0BF{zBhO>UECAM$+YeOJ$p1G^;GqdSBVUs{2!f&>D#N2rfO9Bn0d<mhe1OI1eadHIcA(yhr16KqAbjvp><FQ'
        '*HHRXHu+X~xcjvH`V-ZI1J3)iX?aa+dK|Vjzd5DLW2H$lP_D2C{a|zvXYfQh#Z-9p#ILa9=-g24_*=T1Nl7@?U#k>lvLkC;rO~Uc'
        '*9Maiyk{a&zf})Li}@x7`k}sd$=Doz7x8MbHP<j^PgwLQ;iAKovt#W^{>B$~w`tN^#J2Gk9^q{w$b;sqVWix5FsTm(EB4Xa<18*t'
        'Ij3X2x|nR}yk#UACT=8;8GM?=9NqWnjpahw+w7}yl(3Ql<zf$;fZtnU3uq~yR^Ksw(^BD!ebx8uWYf_JXnSm0A&$AHkCz%!+<+91'
        'P}iX@(h{t}{GHeat)9I*#F>bm5KFeUmR-!ZuZRlY`3GnK5}0AQOPhapr#n2rKCT$p=XU1qbFNYtA2Y~FdPoU9uRk0r!d=q#HP&*i'
        'suDf!u+VM(!QF$@?RUx(Y*IL6Ww&}BNky=Tl-q{Q@f3G$K2zdBT*!^L@K_GW9)z{;Q!Q;cM!GT=uBA@Mmw2$Wm<cM9bfD3ng88<b'
        'x%PM<celMu!$*|M(KRQP%r~W2k`^9CHVi$V68enRqT1}Rn@wYLDo<nb>yEK_+^u_^v^h}UT@=NiFNi|U*<N@!IK^Eg;L5**4bXM('
        'J0oGGP)URR-jhQo6ne~$D;_ewvXL`#Pvh8G?6W9ZElJCtlPl5-dhmsp5{oMNm(Q*mgG-We<-xA5B8SSFh4no|VeZM-{N<_TzZ9R('
        '$@KMq_(fJpSf)SWQ)`l`D?D=(AT+wv)9JK8{X3;Fp5TFjsgj~rR4$zWHGb3F)(1iF9bqokwM{7DZpUHN-L)fz#}?kCmb+0-JfT7s'
        '-e-SW{BN;`9r!?ey{%sne%{T0ZWVsnv8(2C{}aFjd957j9nPJ)mrFdZ{g>eDUoAAoe+OS`z;B@d0Ibpf4Zfi4b(Y;n|LHLtZ@-Z8'
        'O0F16GmnI54zUgENB495a#6=l1olTxZvl}MMouv%A8eWf#f(dqWQMX`kl~gy&nQV}wxB39#a>(@vc<_vSn}7bkG2_Kdk8oNbDw|y'
        '+tQh}$oax6Dc|BR!@cN2{x0U~wA+2U_4xC>>j6JCQT1=CZ47huD4+hAk&~QNk~PcKKUezdg@8NLB3JLwi@dQNu1yB}1qr0N>vBrm'
        '6;exNGQbm+JCjC!*t=lI`IrqR(?$qZGys#rY$W=oINVC@YgJuduKKH)XHt!?KC_12fl7KURi(0Uu%wkejQEnC=5|-MAHpITjS(h&'
        '_!#Wv_~#@6IRan%2R)6<wOL@m4;f=B8ST%sNfUjq8gvDA9|JyeopdgrO<hkXa7W{!m+1Uvq{2JG*eP*R1mZ%(_<UkswKR>sMqFZD'
        '1A+6c;@B_37d7D-ut~2yH3fkYW2z8w(D4d{b2_b@qrldWj>=*5?d^Rsv@|E2`9+lU-FdnfFG27Fj{*TAI(8qcbg;>kGEgR{30iH~'
        '{fPFv*N^x@ev!zZpcFt%Nq_VNsLV`r$tjK+OK)@~_A?l|U>@eGTHW;;x&JHZ_Rt=yCS*_#CAgDXs<GLRQu0e~T>dL$Frg5(N{EJ}'
        '3S<~;pW6v(CNL41<~j0*5ekRzBbnQ-)ZaZx<9h~3_D^7wt+Anij4UG@{#-0}A;NC#stxx=D-of2bs7sNHkQ(pN1p68lA-ssBe$_4'
        'w)ITzO&-<Hc-UoDbLoT8sZ?TRO;qv5GQt5Vaztfv40EvIaHy*eE|C^MYt_J;7{*0;ZJ~IB`mR^TSvf<2&mHw|9W+eQU{Xx1ftvI{'
        '`>q7y<5#8j`8DM5$y9l*70URU6gSO8xo2fMd_+Bu*VGXE%o?}=+a@}dMwNHX2cj`XAxtjmst^<~l`@H$%s>8<m?vPiL@?);Wfk<n'
        '>qdGb9?k^6mIh!L4ACt0b*;|3fvh-1hH>Q<O~Q=f%JQ!GM%Lhm*<Dd!;r5#7f*F6Uq{<u-GYgSarYhZP1AQ)-^RR?b$YLw=&>1lG'
        'RZFJq;Qaj37@@?V5-D1Xw<Nb)CeeYJ_iag1IBm)Y3HI@!p*~m4lxZB8&6lNzBuN`9n2jY=0&h}#AZCatT(Hp;xnBaxqRf)p9!L|f'
        'e0Wy|TQ(g8mvv>YLCaAN07|dJD5yL1KdDT%r3S6Q+%t0wt3VA;8m$rGWpCbApRfaW&m#;oJeJU$+Y>5sP#LO`A&@TQq0SboQ4dge'
        '09nEh-s(lkc9`#uURx=kddYI6d)##fdrBd!mO})mFqHA0Y@!q4%xJa6U$WwR&-Aget<R+Tq|0ySp(kHNqfR+c4`Eajc?E)+J&s+B'
        'RD{%yG6b@zcHXLMOHZO%e>#EufS5UGOXzcP>$+g};c3Efc7?bp^=<teX8LQzVBju(H;SX+sfE~#HiQK`qIhvFSR+$AinT+L<3@~Q'
        'bfZBqEm4>?T`r<GyK~oi^VwmtxjW3RGNVHHjJsJN2l3a9&RY7Hkre?y!F_pWYU8~>S`m+{<tw}I19<G$GJF-~9$9A!!Sd8`jIT%V'
        'r&aC#G8=J@8>pfu@B5f(K{tL2P5}L2#3ek^cu_TSmp<>)z1owo@40#PFzLPW-U0U9>S5~bu9_o}GM8Bdn>EdFnao|g6StE2fOQ|Q'
        '6?)@&VkIW_^!?rAPrtWsM>bvesQX_Kwfg(R$}ngo;OX3=4N7R3dpmB0QZXW|f-j&?yVU}CX%X%E((#D>S3c%aHLLDLq+Jh=G*E)k'
        'zM|CZP9L3)o(azgh3dHcj%dm`ho^1<VxqEeubfO@Jn=4%D#F;~b|+We!Sa1tn%u34H<VMN^Sgx_<*wJibUcoi$)9g2KJWzm;=O9a'
        '<hAMij&uPrWa@D6{bnOjdgWq8s#z(ABJ9Ieb&y`NVm-OBU7vrx&e(NLVtnMh?iT|`kXtMg&Tdv%$A{3A9yPqcsG0W|`uRvz$MRKp'
        'eOEl-l)yytOKf!EU^11AVSqCEJ404HVl8KhjwP(M4!Gdi>1~hh`Vdn}Ujr70LX}yke$E-@N=N6oFZ17N%OYPhR0h-qQ8;MsyOP&d'
        '*!1JzaH`IKS#Aap`cX0;)7|hg9P_n6pJ~=*I8X%Rp&txjTgYNsSSrI;KS4$0o4#IwmX1b58ABhD^ndpw+1t=oW5&~OPfxyD{qS7$'
        'IY?Mwc2jQ9qYwT@g|aUZ+X(UgbBKNnW!<v3u7+|BCgL!jr6fe;K%ASq#Bg+*xfyQ&bhx}wkI9v|zjqFMo31yiGy4_g-3bxyozJB6'
        '&d&^Ffq;zOQ*IPL>x#4C&gdERGU0c2pwQ{ZSuIHlO2s5X%_ER>n;T>zPfmN^nA6yIdj>&Jr3iBRElBK}DUnXAVxZ<<N^D(yxP*18'
        'u8cF_XV%jjT0Ofp@m!O+{`ZhQZS2kK0Yr_c_^5>9C%rNKQ-#6$WL<OroU&%C*!a<9Pgd0t)Z)_lH){<F0>8Znw5)4u26J$}R;^|-'
        '>h7DI7|a`Ri$zOGFO4GQq`~wVApy>X_<HynUR1vE#Ic?(_e3arj|`szkLRv4&r3ayxs}9yAl~RWY5-=JgN#K|t2RF8ro8wK?nJ|('
        '+2Tlz0-2BTuP4x)PjL`k-=@jP%$W%y7Sx`fZ)$KB-q-i%HQ*Ezv=9pWio`LMguB0-au5M%t1iq3<nvq+MuQEYifn}Ga*pnQ;8=48'
        '_uim(xHzG6@NU6mK=emu$P8V7Ofxc0NGfpdAU+^x;E1<L$DeVGaEDR8+#<PPouM~X3|!f1Fse{;J6>eSbRVbH!|yX!SVgUFwx%+T'
        'YEjBI5B->U-GkHCR{mV5$0_iz_#l$Oc^11W64`c)VR-#+WoOBud04qOkifoc(58~TrFrvm2y5pe6e3m$+H3=~S^(KvaSzC*aIa8B'
        'kQo3D=bf)OTBP|qEGoaZS~rXuq_@qfqREj}A5AcKM2Q&d^O5eK%LgiobB3DfmbTh?u(J*}hefacToR_92HxsSW#hrDt*{RXx9W<1'
        'e2R_OwIx*75}29oTHwx-&!N)<z#^c}#$&MEi)m2>CH7_a7CEvg&l{>0%@<e?m)i`dsDVI41C@A<8!O1XlosfRkYq%sS)s@sNe@mc'
        'Yop>cf-rTK(C;)=MYnLJ;-t3RPkbw9Va_mTO*eW-N_R=KU`}n-f}7RRh9@*MmDgu4h*5&m_CG;}$P=Te>Nk9<gj#D_C=xQe%g9_u'
        'mtOcCzhS91jhIYG8f|RP{he57s^eglgg#|Kor%&fO<-aHN^o8$@fypSG%Zb@Ls(-!Tjb{GuLxeAs+4aq(~sh8E_d5Jk`DD0kF9qx'
        '_A9t1ir^fWayMpDJCPo_DQ*}nGmIHJUQLFi(-0g!r-kGdo58T(j>mM`ZAgbwukAGB)2#XJ^S2FlSSkWdONt7zK{QEkLrF|1Axa3o'
        'C{ul>Eu@ym1xJ=uvgMk<@T)o6`qO~lhE7r`XY`SmJ6-v*{%}qHEqV)5bTWLgHZ7i8dX$z}F?z8sZ>q<g7HuHGI0Qj)%)6bDsbhQd'
        '!&SU=fj>3QJ3IR(VgC;7Jn{+EJGjZjLfN-pQlyJ2G!RVio#ln)wt-ws_PDx|1e3#`hcl>3&f(efN6JPA4JMg0r+#-|Wq^Kr^muej'
        '^M1qfY75@|bqMc3WcDst-3;}%4c*C!L+$17t7FVpXgnI;NW(A9OnfyYQI=Xp6{*6Fuc!<R<KznY%8oAxmC5whr2yg3B`+G4>fc2v'
        'A4Zl2Dhkv}5p%wwmFgW$zs(I|9DlXn*B_+k@3QYXj|#__c7S(PgGC`1w6fpX^?YcEdv2pf*!k9(1aPU$Ex@X^4nAkc(WQ)Zjb7D|'
        ';^G7ZaZmq3`!l?WYdwr~ksq@2+4J-$+grNow0>}$ZYIPM;1;swAYSC@8Ekisp-SEn+jX6RNmHC4sWsfZEFx^tA4p*^t5{Y|)gIno'
        'XO{PiC*IF1f8gwx1WUs~1&fC9usdO4fm-b~ryT!RZLe|bJ-v>K#grU=14~!_Y6JS(H{0Nf(5mLy4&$Q+ZSo&opH;9{`PpC7H@-i{'
        '9R>K`x#2xSHFZiuKdxj$+`lRsnQ8t-13xO?Fw0~Cw82<o=Km!(`PVp~z`t{o3JZrF000^Me{z!ym^fY0<Wb*k@7tFQGxG7HYyecy'
        '5F&oKn8XSX<U|6Z3Z!MY=st)Vof?&19*w^eqq=Ezu}ig-OR6Yc)32%k;2KDr0!T<SrXwJ7M4_BSnG@dKOTLcQj@uVmQ*xF><5o*o'
        'D_6PYuN&9f>&@-c-P5e^mvfjxX6pY<KMbN-7J^2TH99nJdJI+uH}oVoJz78VFGoKiV3>%&Q5v3;Zp_Y+j3=<wV$q*#jiLufzHh%w'
        '#48GOMUdRuahIaqMgQq6X)X8UilPaJv3^Wv8gX^>ip%W?U<Neh5X#}VU{QS4er(t3sv&%Iq#(Zjlg?1DP*dZd=;CPqAYOxl<a&<y'
        'O$m`dQ8b!NZff*&1>D^c%U!<w{#`qBq5IT9n+`Q3Mq*Q288I$swwVttAgr_bGXi3u|HQ|LjGvnfm{*ID^8UIkU(o}r@O=}!m91T!'
        '!sX6n@G<6h-6j)@fN&IXL*Kg4K#7?Arl#tWR1Zr28>f`*5yrw(L=2Bg;3b=WJes88BEi(Dj6@yG3_b3<j|g2&^!Kx^`$bp?a&Uf}'
        '<UJMF_Gmf)tY@u->X&q_6)MvU@({`Ug2iw;Mck^rGyLN-a1*Kk4M!`CA-5Mfm1~Q*ucFYxij9h*@A&65$Z4c(F6?wQjyH?U6>=b2'
        '?Uqw>M&-^bE{RXY1Z^>@`eLh#+k#rM)HlMNqGwHjE|nJV1d&WO<c}AHvGm=Dn9QWuP|f4sXsRHal(wM+hQeT{-#iqes@l9cwiRij'
        ')w4pbF5S)N*^UT5NkW<JmmMUa!m#FR4^*n(((sX$c%~S0XvV1$x`PnXAU}#;oB|@B!EJrFD65{_Vpkb?K6!Nauk4X0df~36zcpUM'
        '=v=H%sQw_S1Vy2J1G9e|8lw&`Nbb6aNeO(d1@wS)U<~q|jb}Yr{_lk>+d{Ez>Fm$)wyzaoZ>i_ohJHwYM$_|+@Upv5%}g+4&XH(!'
        'RI8$jD6fEy`4B^^R^^$=B4hy>en3#42$Eubke=FUQfaH7dQaFA|27OA1!V0P>cNJIo2w7_*Qat2!8YLB+80!Sy5NQ_DOK#`A~F6('
        'n22*Um|g}qF5Q8up#|!I<2<$*9!6**HL4PX|E7q%n<i=gROynxd%8-C!p2^vq`#S6iVY={nqybICMh_9wQvotung8U*IoU%GcJdi'
        'rDxpv0Tn}m&%rK-ap83uC4t$Qh_*Yx?UL<vyf!`_fsS|RI-Ai4|2yfizp=gwdm8N3VyXwvjTO!f#f%)q`)7Rs3{B9@389$1H9or='
        'bl<E39yaHn+Tp%^so0oU$tuJ1XmJW)lbYeupGP1pj4%psy}pHgO|XdYg3c(4h^2<89Ya;Narc*5eWcMv0dmmrQBE`ljxX#nq!N|+'
        'M1Kvh{bTl=Iz$O<{(uC+X_+buJGPNHUyJ|*nsg(&S~fP?cM|7Nu-w23yQFL<WWk*=DT>e@jHrl6J!Kqg@i(=5Anw3>yHMtsN6-hq'
        '!~x-ebSbilGufT`F|~QSn?5Yr@xA(@jEe22WMea4t-O7+t%#JsI$@01nG;K*>=>FNgP2O4>k~F5L|p4{i|uJgNa>uaV9d*c8s1tT'
        'V8zw?BKbh^nGT^MRxvlo!(8aTt#-6RCY>2y%Le@X770y6@L5z4C@e&$=Qg*vidtb$K5F>3-gDp^^DH&2<_Ha~$7V=X|4Lo%IYJvl'
        '7w6j}9vv~QiMecf>~ub;lg*ACkmYbHPZ4e*mDOLyfz9~J^<7e;U!qpR^fc*la`SNkN=r94z1KE1dsK~n?GN3lqZ8cnLG$9qdAq`h'
        '=|OK~@m)(fu4zS?7u%PDW(>zQx@IlJnPlt+w4tgo?uY1!t|gX;Qivf;sMT+tZau^e00<&uUN1c&Ls>#^TgZ*IEZ=PP5(NUiK=;w$'
        'Fsj48$NU%eG&Mdj>gaQ3&==Okl~fXJtFV}BiVGg8lTR9I7BLg9s#(d5FPmQ_x23pv`8qbuTl(f0fT6`$9t56PM1=HS^+(zq&8V6c'
        'H)!oMLZLCz-y@1aVM0AS9Fg?NV&{ND(hf{!QyDq}-yiFgDT|VJHc>62BiGK@#`E#jGxJ-)6D0|}d;5xTUxZBcdC!2?>B6$gU7$DS'
        'UzBCt(9Dzh&NPlPbY;vP>LTMEL>;DI0~o*DLeo@#@bA|%H|6Mt$^mJ`(!3WJN<7c%P{XzJJB9}eBq90JXgCqkc`;!a%!jXnF3=0q'
        '3}hXM_Ul)YpRX{JZTd>rfI=<jb7MZ2V_Lo@1DZ`;?tt9K?LBWQ-f;e)?J)}7yw|(g`v&LNRBh->ez^7Wuy^M9+srW65{c=0dRCF#'
        'rum{pXoGM4WcF5@F(qG(vF%B^(_g~C`c-DO+t}-CH%SY$3H8<87ej-Q&i;`77?-An!9$IY=7>!01DN>jMqrC0rskJa|0J>y?$;9R'
        'jTEbo{xl_jzK5{!zb>F_+P<LR@o$G-_G5MF5Q;_|Dpw`9H2W^0@@qyGv_U=RJ{p34(!lJ}hILurH}^E;bPQXp$HR^%BZbkPz=6!B'
        'I{6u+ld$B8+QJ)jMUM>o!eF4KF|iN)d@Xw7c2CLk3YbR{(**u}z&)n3v5jg~cWbx$z-+eSNhpEkpV*zsAI@*4GMQPD=>7B+dl?Qc'
        '(u+w{Yoz9k!4(5HuS{cG?MD*$t3Y^s;7`0n5B~L&Pz64lBO%T{etu_pif#GX+NyWpl_Ege{Y)wxR*i?RbH3baZ+iGTz0CVd(IK@-'
        'dA_L~=a}W<R)1X@RWd#6xVi)eiP8yaw^?IRv7Zr1Q4npgsQkg<MLRnn%F^a_4_4J<ijNUhdwdL%!YHcETALnn|3t4&4NFHo&3kUj'
        'zL{Rb!}CSlwIkeQfczfh7sdLucw|}>hbk@Ra#p#X>l^aBe$ea7-%6)<^?M1<(h#WAm0}VBPd;hn*LvzzvX*X+m?2SFG)NDV4H1N+'
        'Y*}4cJvfS}jCEChJCQIdsMigH*~2j_M#9rIq31a2*<GyQ=#+VlfroMo-ZiiJB&74yi_&46q-pz*+F7Viw3mb+6`VzO74fV{6%FWd'
        'S^$%=)gt8&uB-12ed%K#p?P6<i}2aNgoRdJJQGIfRSnkUSK<i~3AZmd^X2EbBLesP?+i<2x10j?6(=;{i2M>n^9ex_HL8+Q#5oPf'
        'tlZ5JvJm8i+8AG)-iw_`Dsai-719J?Ga`Bxbh6JuVm@3Xr2;>Nh$^KuckUQDBB9a<gYBi@qc2)&+WbV=g2N}eYj^lojCOk|UVe`o'
        '#FsB)9OBFp71~AR81r*VGHt@+e`dxzUanwG=_ZjYe66@*^~&N{$wfR>b9~VasY+85mQEIy%&rVA1Q?UxbEPIIpZ*qa-XCEvTs(3W'
        '>Jla+*RTsOTcs8_j7>J9xVc@)7^^sD%Mk({OHSy3`8+QeU<j*CVNDjDc(`<V`E~o+=9~C(ulD`{XRk?vdn2aSa*p&lJlRsetA59-'
        '!AZ%^P5xf_T(IbOyOkyPbd96=r<pEtYx;6^)0s^Mn*nyS@)@GXMP}RMUbwUS7O=E4Y%7On?9{@d=`ls$^f;4)u4|qZ5D~zKNDG37'
        '6${7j8O#b(%INDsBEMCiM4>jlKTV>O#ZsQ*I%_xbqI|?N9N4?zUm=OaO|J*EJ_CT{Q|HK$xh~C)f{2Mfw2juK`GfsCY}0m~A5wcY'
        '4mScaeYx_F*UyIuX{<g-%Jry%5|rNyz8jQ<n$6c`Jw{GJV`<-o9nWyeb~Qb?_MdfBzD5uFN_3gw7haziOB!FVFOGrM65j`JlWl?G'
        'wzdz-T#L;>MLtJ!BisT#G~%S`wdB3V-|o^uQytE_dIc|0VTgt~g{FguL^x$0su{;Rp|1*;y7(E4qtn0+!K~bk({8xd*M`U0^7dZ`'
        '=~Z3k$kEOga!%;(rY}rB&s#V{$AEarC$On+!0zS)1rJ&-Y|S$B%)-=|2xS!EZ<cMTuTDMto?FMjuYnA9NRGu0Q(2wrv6!dFV}!{D'
        'pWI2yzsJRL!5h+^NuFe`cy8f(Y;vWmj}Y$_#Yb4XRIP90I@Uf;o(V)!RCS!EQL(s1UB^K6D%u22_Of$=<t5=!(rEz`tcWcHBcNo)'
        'E%5OZWRbbbo#ar!(m^Mgv)_Z{<SRZ9@pfoJPO$%c^TOqf#RdAN@^!G9>bO^;Bc*tjeG|E|TA(fdXjRn7WcSD1fNRQpsCiMi$HbYJ'
        'fQ~Ypuj@yr#?R{H7@IM5J9noscexjHO+Z$-hMw(tBl(NSXj#a;amnmpu04G^)+5pc{cY$N5C>^+QKvZvzE8mFhVFix+p^bnzbmKC'
        'Y7K8PWPTbs>#35X)vFsNv5F)ye4Mooyll*-l-<2PM>KmEb}M=q@pKSVb5-w~V~aJvgcg%&Kc_6J&~ye3z47>!!`9E5%QL>UcoEMj'
        'GkTZ0n^%onkcNZ5E|KJm-JH3KFiVAl5?QOlGz$D7ybJa?hEd1e{Utf!lh<DBRJPVMZ*Ee7Z1^x+rFr1(9Ib-pZ`0E&zF*dLVe|Dw'
        '7^!c+XYs4BC-^2Iysj?ZWNKdAcaKk*eNRN5o1Gks3j6kBfccPGwOKKsiKNIUWuuKPwGU{H(O-IJGv04OhS)T;T+ri1=q0x{V<c8p'
        'm{$V(>Ojha#7416Ac}^Je_&?Qj8{kDMF$`uR*X4C=?@$D*^myDezyn~-(7X=PTC8<VXBEyP);UYT2H*VT`O=OQ<jusHvIABmXh+2'
        '0^&w!f*Wh!Kr5bx+CgbvAcP%|CX-as8X*^Xk5M_U>*5k$a*1o}MDzWOf~E`<B@MJ(1$W58&tv&Y%d?wuD6%YMV_!iUacoDP_%Jz2'
        'CS+;xKM1EkARL-B`4}^d8v4`oA&#Eg*f3Da>R`S0CofaS4&Q8i373W;W6V4Drhv_+u@4+Uc*7ie!A$WRlO1nYa{ao`IX{p&z_y@e'
        'Fx>HrWo`xYA)6i}Er$&u-KH$*9E5c|9;+HMGQudBSdo4oq%$!o?P?ZuI7RO>9Th_~N4t3OQ{Yqye7Dv5&NyuuJpFlG7B}!OZ-JM8'
        'U8}%aqWH^Sg{f5awRd!aTem<;RcUd3bq-&y&KOio{`zo)+V(V0-+MC`{A(ha;xkcXsNAl<I(MD7Yc9s(Fa~BciXVzW4~z~XOD<t$'
        'g~QvC8?hV$1u-Nw((4+LcOx>g%%c>e6M4;fYcv-851@F}><=sb?gt;O{5MY9UWF{lNa3n&5AQU^O#DeEycPnsb2iRoI2eJg_0>O{'
        'y*9cbb`Xbds}!{+#|3|=h+e-UKP;vo{}~hLp6#CKk}gTWS{ysv^#&bbV`QPcQ|pI{YAC<IkZZ6hWyZ>GU^UfIAT!wtk=UhoWJWmc'
        '6!!4sqte}Y-LvQN81#W|w97@6|GHc}nP9DS+SsHv(m1s4xPWf~!tk&3bWiS4O|d+5>TBiJ18N5SGNec^0sno@rvB<uCOK%A8>9pt'
        'kY@76PRKG+{h)oa@#fI1%l)O=OHc-^@K%5N%`Hfg%$W4Rd~?$?yxDRtZhFpzkmFxH*;M#J0z`Oq`5XwDd7{<HM#g66!>Q@8&1<Z5'
        'Ii70Rs>uUVe}e0&VXTGla6y))g!TA(ix@A{AnF-Qm_i#Q18&nv<i&1p_EjyhJBs0L$nKIf^J48X!O*sBcDb#H%+!)|j6s}vD#pgL'
        'fKr5Cc0X7vQEZLOS7kl<w|t|P-Fua7`g+-PKYq9ExW?@Z@nAUn?w>}ps7uMm0=;X>deawlzMkKu+BQ};gco5x)e|(9sPC;y;B2R2'
        'q|G1$@o*SGMZEwn;r`);kLGY=*mkh#J$#C4^B{0$nN`gWDD}Fky4*Tf^+s#Jge<e1loQ-ribbhgn6H%rVcVxl9N#XR{MqJ)Yui<5'
        '=Nxc0s9rgHS#$Rl<f4g7g~cAmsa1ESo3|~@GtXQ0B=yJ((l&l~r4{q34Q=qkz*GLzXjbr9W{>^`)~fGulKwM-K$iM-s%NufBZ538'
        'ZlWUgzbTmLm>*Did#H;IKi~gf%18hD`_u2=<)cA9gGm73pX$Yb%17Q%X?UW|KeZJdcDp9elH{8L5`<7es)qq>L1dN!aekoQ2hb5d'
        'ZFzV2?_1=LsM-pzokSu)Xc+iFiueU(Wr43k#**sDg!%)_M3=@boqsi-dX%?aPiC^)SzOu1Jb%8AJJUOz)YMivoiD7NvBQu^#G){n'
        '42S;znEPI2Yc6cnN;Q}(jfuGi(fuFks+@Q77i-fqa|BF#{n+vh@{$N9UVUX*uLA|=i%xk_okJ-|OeHb?hw?7u+>jPyG@C}M3$Zs='
        '8upQNB}DXq5{=B^PcrtsgT<{f3T0v_SQ#YL-!LK0{#@r#i9qfcUI>tG>o#Z*ammqSIKdPe<GIki(XXEC^+OpRQ&R7Jg|ii29MZKK'
        'J7VKZj#YeW7q=4Ev2!m6Y6*hs&3!j`)v&K!x`wOOF!qE@fRtN<vpgoGQiqIZ&lhw;19*a{hu(S5FVAt;t|aFOSY=0<w^M%xaqLgS'
        'kB<Ggy9AIH$ne|*zp&RrL!9E=H^`SEv3667A8^~cC=2OWhF-qZvT2wlO|!D)%HE-puvg_t03Mz}DF-*SPu?E*?{OXAD(#{AhIMtm'
        'PL=h&lx&uxKkXt|g2bp`esKc2fN=nakL&A8zJ>r15T<1rDdvh88E`U-fP1b~teFvGU)ZWLWg9G&1LR4U4}RT^dobI~oNh-*mse!@'
        '!Xo5Y_GgQ`f*YNe1|Mr^VUP<$`UWNHp1r;)(8a)L{){`e<?^D!tq-$t)B@UppTBMn=0i`A#WHccTOj^zOL_kGlDYDvJnGVPlSI`)'
        '$UJ}_QfS=+;7`?uCXfi?+>gGO$46)d*ha5&<F}vE?PD7_gcsr9lT;O`b8V<gOw9RG9sq1yU2-T^EXvD6AQLF}fChkszs6Jk24=*%'
        'V~_`#1|u{JLG~9T)K?I{hT%`ma_RGXKalqS@`jG8RY}!-(OlV~SIs<N=mk~ZAvdQ>a6fDm3L{B6lq^eV{xK0Z&N|`UOA^n6Uk5p&'
        '>kE4qht8k%*Wk=sf!ier%X`atBIQZ;BkC=u^QXI%K$Wq~qi}AuTxU*nZu+<wYTSjM;R6|kakKAJ-~*+T<HT^Yp{dKDT>|sF0Q<!4'
        'lXyZP16XS3^{<pzQ5qm5ZH5&gJC8ntJwJmZhO_JUN3dFNw*zf#NZ87@HKK4S>e;_J_H%K0J*BAD#!GDtd#zXD>cKJgF;;g+t%jI)'
        '6{ag3)&oz-wvF|$gVwdcsdFIQH7IQRLge(~pSdG4UI;mpo%?@%QtB_#5?)(vdK$CD$=0LwRn`L&V`w=sup}b0TykpvC_PWd>K6ar'
        'C9ZR7EHKHJLQaYK%MrG-GIF=t>qy-7eRIU3CTXBO3$hNnnF?txTcKa7X6Q%c%M_rYE~n2Y^l==B=Z#);^aMh{3Gq6&4md9%&Uoqp'
        'dHO`~yIuv*LM;KVo<;k-AySzDl`{wex1pc9a3CV46zN^>6uSE!WJV&RT0#b<Qu*~E!sV=2Rv0Z-9wJeG&(8)m)le*cx>hOlcmp~Y'
        '(Pq<v0lwhcy0E!2mHL$TqlN%k@q9aNFl4FC&*8&}+ew?7{Oe##^2MK!Z~U)g+==JDlUcBzaWnyPjjOM@o~sT#%Re}AE&iPt!aJN0'
        'v?wC53Vb3YZ$RqiyeTZwWF@pF0HQuP7{+qDqb%Zo=H)n4;~1SDhd`%O2BJLZi+$NouNto+VA$d2%BrKW57ZmU^{W~zl)FLI+ZO_I'
        'IkBZ`5?~4TWTpZSCE6kcE5hKttiktSjj7x?as00Z5|$Ye8OsU?lDTxg$(RK6_^U4IL*HVOg-v^14{h`tGxW1u3?=%$5d3(3p}I0V'
        '_qI4-s>l-n%g@2)EJL9pAs^~_i5zL^0Qu#9>kJ58Zjvjr`s4Z(L1jR5h=?>EL@FD{M=-<NEL^JrZ|D2gz><Vt;_N#j?EQ%VxVTXH'
        'U@#-7Hp0Ra2tFlJy|2?>b6CU~DSoTL^J&5P=!L0MT<b85x`>?UqiP!7oUO5S+##sRxUOUFeY0z@Bl})^w5>`X&=z~Xg_kYRl?6=d'
        'KwXM1yVm~p2g$m3(DVi)I9W-(ussqtlF@(^A6>T|MuT57qJ}s7`^Jh5B1)<Ye<aQlz6U^-$Uw2J$`pwQ7ZFbi<N5~$_Cv2KF>Y0*'
        '>f=fM6(z3{d)K*5md_cd2nJq=${KwD=bb>Q5h1@cWeS%~t1``Cb@lM&<mo@nA=A=HgG~Gvr*M~Jbsy4U+IV^IJU)n&BuTYQ;j<$)'
        'R%i1a-Rq?3A<eo803|@5pY)inpLi!wc7XTCK9o)W^l86Kz;Bu;5`N(McbmHR4h7uUlB9XjV5r_&n3<aEy>U%aEsWTLR9L-|Bv`_0'
        'pV#e9q$AJ&!`)W}#rXtI?k+A_2=1<d;O_1Y!QCxra9iBn-Gc-R5*!wHhu}efxGYZ4f9|U8^&al=rfMF)s+p;8re}J(d$v8g0mlc)'
        'dG9eU(KXhl>7Z*3=r3Gf;pjBXQQcTmvyUgIe}I|fDBD6cde|NvIl9mS;iu~H)Yvh&V2Qp^R%IC_t>*mUfpe~#8x=W!M1nE+nqmEh'
        'FhWneD4b}b`sO!Rpv<_D<yI>4Qm7gY-p8(9faggrgqIm@hH}^07xi)H>-Hd<FQX~+tw(K@tCAcGoh&OIgq2Z{Z@~dc25=rdC4;l%'
        ';`;!S>0&AVLJRPvR}|iUt*1J(TL=|5uDXNppDLY-1}@lEG@$BY^%c<UM7<OX8Q*A)7|!ASwoq|HwiH#880+T-TNMuq;0}_D-$1sr'
        'E((p!Vq~azsE`ZNmyjssdWFI&<;K@kJeR%tVBzP1)wf5{0oq6@rf4to5Trstf-dr8Hjrx(bgcC#cuaHa6~&>yrb>j$En%WCQ&lFE'
        ';a~rZjkTKBEyww*)1B$1zzDO26`I9PE>L59K8v5LP2)sK!H;yv^1IdoI~K8;(dAgf#0^1?6~GH$^F2qrKi+Sy2DDh04!G#Zd^z0N'
        'Mj_*Xmj@|x0gh>0w&+X;_QWiNU5>g&2S24VEW~4t9SE{nvxp^KhW7>`bp^0OZS#0{e02KV&&pAdbV9U{9kFO2>ZywkLADPn{t)>!'
        'gk%uckvDU;kJP9Z)~H%nZ`Q5)__w)3sb=`|bD-x18dC1GCao`%F{+eeObVOLr0i@b()^gjfgjXilmk`~Pg#-qZ)v%<i!bDi+J&fM'
        'eMdF*L4Z<!T_sjnp^{AU%QQBVjW{KcrUPK?fuf}>V>@XR$T-}Fvn2M0<=`;yOc1xnh+vk8;&sIH(;>^#Bxl!(QvfP1VmfJ#axL3A'
        '+dJ2|zh&H2K-L+yq?}<bh)s$|oBx=r>33j(LMD$|)lOsKc%p@*h&pQk3yjQ$Rl7|%X>vAZwvxgs#j#WapG(>$=0PGzj@K+1p4lVt'
        '17y-1j_q@8B!fxCdpx;h)pxo{0C1riD;2dzrEoz2=)*cZk(fMeEFUKwmRVOp&&m6NZGhl_qJ#xTA@*EjaTVJkIk2i<9Tq<qes!m_'
        '9azvGYK(Wx@Sha}M)J_dWBhMj=YZ9I3bU~k3JDIZEv_sQj#G(Mu<kH?eJ$aM1H(^(6{+O_VX@`}7ZjCM9|f^$c<p-aJqt(zSom6l'
        '@EtQLW!W;tmne04`f9W;1N+j7kRbFHj_=uzl9U%GB7|Dq1Su7fk&5kjT;lJegBzXG*ktJOedOCOJFIvKA8&;+*ycX-i(fG|kTW(R'
        '(WOW@vBX`>ndh4|sQ|h69Xph=38KddeCB~Z3u`s<oORC>d3GQdB*EU-Lsm9J21AC*-c_|nWmH%gYHzr#`Q}l;jZYec(4WhxC~|c>'
        '$sy8?H8{?f`(G-E@(RJ_ZI)+h<@?bgGStqQZPhVh3^(u`buI7x1;(GL7Cy%pW9cq79!oR&c0Ne@lIgO4)M6z@9m`f`vv;`l%L&E_'
        '7WjGWU8BkcVKQMwDGg4|SN272(OIiy_)iVIPnzK3#8nrsuv6SKl~r%ZKnJb5q{%082p+%)7P`kY?Q?`lqd{=W#qoY3+3!6=&Nu-x'
        'wsfY5NOJ}v4$!w46tkINk0bU_4aw)9N_;b4!?u>92fL_Er}eOMLg3^ZsoR!6e4ouTw^f?wXaXvTgh1xU;%gfgTWVgZeTe!PQY$-X'
        '#SR}9bYK52_ri(QC-GF|-Pt4Gla{59P}}@)aUB_i61;@eO21>iLUQ&Y>xJRT@{$~2O5u$(e)!W`I^^RF+xG!VzHoY+vU7=!;!Ypa'
        '4O?(h=FGmSye6j{8;E+;R^$AT?v0uopLhS4c(NCZ8{-}tIUs|KV^Lw&Ga#*Y$N-9Gh|+S^b<~fD!*AygVTGCXOojP<#h+{!-md!2'
        'fXP)59LFFvF7KXG%-@ZHMLwXz+SS5k763a+hLaf1$}F-q{doGD(R1b;;~T5!&7wf)d$>XLo@pnR`R)WT`aR}>6y7((t$3C;<<V`*'
        '%yhh-fsLCr<ysYm;(Kr@*kR}tTo^4~%Q>shB!&$esu}!E`h4~9x$|n2vlG)B>^+Q1Z<Z}0juk;Or5sJz20iaTeHD>23psS$b`XW@'
        'c~Uq16W53RO(`_Og!M^mU=CW74vlRESxX+>7SGXaCD0{@eESM(o>t6V0en{q?<hms5=;!Zu?V6l?A32&DO)|5LO(BcrYBxNXPyz)'
        '!8f*TY;o}G!u`=Rsq9MZsKgY79C3RU|1{)1EvJ+BD&=2Jqh-8Xw#?j5>nw^zPdXPB$DIT-*)<R)@X;yR8*}nM<;``SiKqxJn^na8'
        'j1Tuf)Gi({3~OIB+(?}Yxffz!0{#j>g0-JQxXHupM*+Xkv{Qlh8liDwpSf6(j9<QFNB3<zm*oxaDCr2_cEj6_A~$S<m0!^6Isf_e'
        ')=i&Y01nF<hyW&+JHW#$Gd`7J8B_IRDoxMzPujVt%ZkHLMdv|}LjRNT8#6a-9m8gl%z|H-=pv5mvOI?$(55xux3<wM$TlIGiFyf4'
        'mjVQZy*th@f7RR!vp|U`iWJZT(cPm}3E`#3eTqK1OxT~F7;gv;+uyt-vKVRb$>0{30Zb-nd4D_9__>ISN1Lgul=|zmDOZvp{*&-2'
        'mP1PBkSIY$8s_~%y90|UqPrBnklT4vnl7kE{}|s?_M%CRAUeO3t#DBuuzBSHPPczhIkTJau*(;G{a3=0ayF&N6a`T-tU9LRp@v~o'
        'rEdEaFx#8|3lKT2u+wewj_ugk9Zo=na_gZ5;U44X8#tF1rm-x7j&*m{quDQu6nzJ;ETf&BApY_M)eE5W+45lWeY_gu(o)+bxlm=a'
        '<wBq+IW3QMoQgJkad<dcGCbYyF&u?De1u=%cn%m=FZF+6;YVwwKRj9$Ejg<2ShRVZ9#Neiyqv*9tci{rOOmfhx+5gp+S^Os6nPSU'
        'Vu}7n<rte#?mlhIgSH%aLNf~4AhG-};~12*GOYQi?UrGUIAu!1II1Z@rqYO>k}U7FVFsNXhFNxUF78GH5w?v}gU`zH<*hoC=s-g_'
        'd5~}AI>);2)9sGqw2*0SgYKNnuxQ3tS;qDgDwPu-%Mw)>p3HHUR6_Qn#JRaJv1|t?3xQ=}=tIHPCx7q1eWM50Y#-`yN0inEsq+yG'
        '0}d_P9)7BvowVF}&s4WGykuz`x(#EItmnvxT%Nc$>JeWG7vnzE`_k66{}&jguV|nliQ5m@`lSye^Ag2FGV_D`D1Ktv2PCqGsP<5!'
        'i_Fa!1))i<Fd{rW+rdBN`Vv;s?Ny>8-&*3Pa35CB)V<<8uZ0`P%St}P%%WCOsejW#WOxFNdzCyfX$O#{6jr2S>aw@`+S>$m+w4{Q'
        'DE_-UCG7XYOhY03I@b=&(a)Lf@n!U%K|CZ0P8%)zywMzpFLJdBzV&>tL#Vfv70sO3t(dtOc7D6(>Uq85-Fc;-lxDevX#Psl6kA`7'
        '+)KDypKbReZ?i(@qm~R;D=8$jRh;gdYX2(9i=;`=LHEJ%%Iq9}mVUnw;fZYiDWsDJ<^2Iqt)ici6Ze*W%;6ce+X*hLL%C(MEvF?2'
        'CU1HUrA+4tuI7c)sVhJUUw#b67+x|g9&Ast51(9*!6z-#Bv>sMS5_dOsW^efw~qwNn{={`Q^JQ9-ds&{;+gfKqw#Z05lGUdLodnK'
        '6L0EYut5iX_TyqH2gXVT^qB!`5`~&jr1}?Xd(n68yzUx=_7J3H9os6-f6`<{SUT}Ng!Z3mLOLAwTZ;N^|F=c>c&fC{{9lT^ztUqn'
        'XCPg&i<WrUr2;xu&?j-FS)(MiGlsVI-XE$aH`u!7!fo>MnfY-)<M)08V=T%u3YR_!zZ=VWR1~k|OT@Iegy0@A$*DvaEda&{YM=wl'
        '2V{|(V*)PnWW77H)5H_~+4Th#nm?lCmHHZZ>96J--oT>E7`P04!am;JJTD@|WZk}B`8ItRWKU{#5)B_PjgQjx)feX`pd*A62hbYj'
        'NR%JcM#Rv=)TW)rhPUAFygq!LP0+jE4;WP6m1iQK{&R%|;eQNL@Vto!xJ_VVpH1@?;};GGzT<tvy?_+1Y@2v)$|^L5{YV2$WTJih'
        '_83gL#ggUPHhJLB;$a&6D<JuR>p$~extAR1K*Sn<+t2@yUAE?op&!2n&d4gd3n$psii=ItNMGT@1^L_J8?Fz`?<I(Z4$WqnS1Pj6'
        'aN6l+>o;2x=+BdUkPntp{xBI<H83$K7Lua#0*JM-nHBf}Trb=z?+!1wraT5oAUsA^4kb=m8&-N!jsQ|KAZzHbhY?7;Dx4x1v=)RW'
        'w1K{OkyXvz%Mpb?Hn_Tue~^(%Cpg14G`as*p{fO20Y$P%30b-vr$oty?mMSu1eY(-2`lN#r<MLN*f}b?m<o5a-qeRm)m-GVicI*%'
        'XH~%tl!|~$bb<(!35qJrUqX%Jq(_&+vL}2x?25H?cH7&8+$(R)2ul%)U-PMpK>qyvXg1fimDH{7x3Nx^=lR7_-rqX^xP%CM+hKM{'
        'NaoC+r{GOw4|aIq8pT`a0gEp<1Nt2*cMsRwe1@rBid}mOUIk7o%x7?jQVe4Q#(5d9tpq3~WHeYn_%JF5rsh-0CZ8n2m02$M?l>kI'
        '=4hE8jicZ_c&vP(==$=5ZTl9&)e>isY(T*UIx%%@Q7;lJadp(ZQjmf!ZV{o?r)aW){-9<SxP`HFi%_NQ-MVUX0lH(vz@rt6PzQn9'
        '*bv1XqM1@t^{mc5#p@mg%?Ws+H5Cz5IS$vYA&>aT9bxXoUEDx28rE;kNiQJjiS2T^&w21NHW2m6$f^)B`e8L<zP66LFWx2AbYVIK'
        '0r%K^BY|s{yYe)p$0mzI8UoCM4EJMRq?qhw5`Q#D@o=6D5w^yto&EHK)=bon#-3p}0Y^V+$$aaG<&xG)Wv03r2pF5E{BSS4>m;A8'
        '5Zy2a2wsCB(KhhF@!5}f`l2{`0m;MsLop@_Pu_G(QO;Asc+5p@&qoV%iyi!)9(U$8^`X+7O5Oz$^QK2<sgHey1)ztgxpbYC%*>-{'
        'CLA|@cu#Nu%j~(_tpMtH3#Bl=h-(xWTlV>8^ieg{E77n&{aIXTJb<|Ot3E{Vsc_`emb57iB^}9jHYjWi!%QjX&#8pbo)sh`V|Hxl'
        'F?CJSTo3Q>Px&(WWlaHL$Czeu`%i@4ZT^3l=JH3ixSx-E7>0z_Q%B&|eH$$A9T`RzTB>sF(KqhY=*bLv5p0zr{mx7F(}ITzjOmcN'
        '8d#F#!gG4=ZBq*r^+u8}O_iidXL$~Bb@f)jio^<<`EvRQxBFT^4wPW65CQ0&_D`rE@UXyuojNKJ55&VrD20$IH}TS>6a}^;oPn)z'
        'NzPN{n$Js%+udVdUD^x4_hz-1m8|lblY8i^LOf0jml5RUgw$h<hSeUYtem0!rH7~4j!PBEm5KwEKTR#M*cR~Ov_PXix+`lT78xv3'
        'vpTm_SiKDSSK8+^EC=X9Ih6zNioa}Mt!k9Y?Zk7KNH!SQ1XshYZ+h7mP9O{~=&OGen}5_vl)0SwL;Re2iJ(jB>c|tVIXXlcZ82yL'
        '!0WVKj;lC|+V53>+y!NqaNv&57fwL!G4t2mix2A!Z`tL1W@beEf{=4~-B}vgI%^&D?^Y-IDeu=rj&CI@_FHw(ABlQVxhx`NQy;j!'
        'N0tdMx`UW>yK$W^2<as_<;_ZwOZ{0H|9(kB;}aZ6!qI_p^dx@vy8JyRSYFP|bmVSv&t0t`?-Un|xOlyQk!ig<vvF^8N~5mOGXU=f'
        'YKJT@B>FpporUh1g)$0&WSH1mykT4pR)2<1O@1Oj)bOLs7n9BzGC8FoYb24JUTj5giL6B9KinU&Oxf8%?dlze_(x{MbCA~TN13n>'
        'gS>>DdN1WE%(S##K7F9*X5bHyydY#w2bb?f*zNa2m~Z<6@8@<TYaf)n8^HRgrN<OlOLJLq)bvTi>tPZO)RHMClRa0-m*~Z$KJUM4'
        ')HOB8i)c7(8$C`9J;wF$fxFK~69{XZe92{M)9Oc4)Jsd@CN0eUf*D<3a<!K@0Kp_;;=Qr4(>|Yj-uwQ!CJvEOXc7Jc9bta{qKPs>'
        'Q5-AD$oK0<Z1zVunhyvI3VXPjoAJu3B1i=aaXD-P`%lfcQSfhHFR>pHPi0NNBc?--tpVYH&n+P31Cv!=Rss1<w^-=GReB6zrVP0Z'
        'V&;=p`F!wGs#ed3;?^}4-E;0Vi9)&U$-1xJ2Fspu14Nm4$__mB^4mEEyxS?cWdHKPew+EA0@S<59sexM^+4V4V*l8<*WJG$Xq<Eb'
        'sNN2p14#_t{!(RGX)F6T{-x!0aVbW6?{LS)`46op4G&<0YPaSG>Ac<F=*o_7RhflX5~vnkU=g17#nCcrK)$iWbo1~#43(3$-u9Nl'
        '{(LHAv3rtxIdW{$i($}7W6!~88P%pP!S*pdVF3{W;J}mpN*8_J4dvI_Ok{G>$H@HfJyB=bK_Q+U){aju6Pp{l7y_f^1`Y&c`EN<N'
        'qtR!5TpV_wsi^(ovGXmvO<p&y$tFD7>b3K#c+;R6hk|~>TYqkhpPTNY<H0o#mc6yxXyVitnh&PoO;#}&#gw#6dFq})f7wGoD(wD?'
        '(sj}J-v9MM2&8Ticgkb?9jU+n>Xmgjz^}E0?LtHq7-ceMe6wF%T6>e~iGZ=<r$y^{sPakU3_<r!q%RCo)&(7tMY8+cV)r?~72C-{'
        'KMEh)&ZWAD9b7W&%VMv2ba6VA&bt};BQUj(9f^B4X?3cyR?*)2^mNSPxz5S)&s=B`>yK3FZ}+M#v}OvXY!OUPzbFYGN8_!Zb;dIp'
        'mUG!I5VB9#kj8HLPYhZOr&)wsi`L%EP5*88x9QW$upi(v_SM4Tn_C?)QiI0z4ugNjCb{=-ieu-$b2(s};UBdlgYj)!njX($b79ev'
        'nWu)3E21xJLR{!D0l45x)4kvLKd;6}cM^wMj4o$)Fg-Gi@rD)sYaiKzZ@OOj#x7-94;v!lISI|M;yyu8G!#lCFr?L$?VOr-XtN1*'
        'l550gzhe&1*(fg3WsY`lE?58hg{J!~FS4eXP`wSP7NC~nZM?40?xcvfoad86wC4_ef-e<*PgO=P-&ABolMz4>)tM!Ba+3iaUap0Z'
        '^O8qIo>(VNFq1fH+aXabk<B0CNqjI^)a~eOwhtevboJs#7h+#zB6m15>UbUJEF^$Rm>-C{cse4WOj2Q2Lc{spfx6YrWOMN4&}ri_'
        'z1QdMnS%Pafb$26I9UoIB){(0n%BKm6f5}S<!esj5L}xrM&Y|OyX~LQOj43F?2ZeTFZC=cGx8_GV+X!j9)$V(gf7JoJjThJ0xUan'
        'b;y!x(rM;Vnc@Ou_9=E<r)ktK2gI2WPPm@EV<{eWw=-|Cd*SnsU4yH!;f<Kq2w(nGm&FPaOu3u2kbyJgBK;816*I%+C;6Gzuz2L*'
        'w~x-!kFGOHMRg<pY)~J$wifZ+K{_1Iul8n;^}BOY1y}HOX|ud@n@Nr2ay4o&M<K^XDVqDR7=Mtqd#K8F(M<E}$>*cKlNa!dO^3&1'
        'H$+O#PV=}!EY>So<sX@{LHMC<Pm$^8ndw?F+QzFl{-3+0t<nlLh^n9;0#Yd-q`Ke{=h}$*tN-{WoOC$Z4}M-`Q?9A-IX1=hH_Ovh'
        'ODOz56rqrBV8t)WO+A(zN`UyZL{d}tAa8$7zqCvHWQIHofoA4uI2oF-=Tldd(qbQPAN8uUMQC35A2CpsG(U*O>k|q@d@|CAghv%g'
        '5c<aQ$Ls#owci!(qgvl}Na91GZyw!Sxu_5P*H)3>R<fly+<K$9GP&)6OMhw=-#c>#FaOhea}ozsL3R28QrRyuED|>0ZqC%5p75ia'
        '(E)bx<Z%yqY%slAgH@523+G}LkJcibOe=^_S?FB5G#?AwX2~{|hu>z<R{UCRFG}H&Sa<hscXy-dQQ_R&Wv#%-cr4U9@K4|R);Dyg'
        'jz^Em(od{>M42z<oON$=-IYJ&?OQM442;E$0#G$b!2=5BGMXQL_Y{sF0t=t)sC&9E$T96K8f;e?Vzw9qS*JZZOR6dN<kE>4Ziw>L'
        '$!-`}Dm%cEb4+~Vd)2zF8EJ#4?GWiWT(A5*|B{B<qK?4g-BM{T;u(F1?fEkv<}%bR-zCQNZbhP?PoWcAHwwB!P=htW23J3fp0_$`'
        'vu3V7Ps8q4!F&aZui*xL%6&f_R9UqH%i%ixP}}z7s7)<3itp5lS*3M!K&U!6o?sCZ`|3WvG#g2NA7y_hb)(>tCAL1Ek)oYj{5~u#'
        'l9=%(p!lCUU*4MEzX)YRzs+U@$i)7k>^+@SLvf-_ZeX@VBYS(CU0B)7U-~0To&Fn3R%R`b9V$6azC_dZ99(-e)4AQxz{I0xc5Z_x'
        'vVAt-@FMaVrJtx>6Q$bOw*PV!<roh-hLKW-p*(1(G#DUXU8Ig)@Vgs&x1GFKqT3jNK1-o!y=#J+?_Ii3-t9YpXw3Rq;qdR06?6BL'
        'qK(xUAcL`A&%|Fq?qgbb+E0ZV$Q)C6l4BO7u>ahja@x7H)X*$d-K2Ih@8^PL6T-Wu{_#>xGg_q>AOmfUP`0N~5eQ=pJ`R0o4R@ml'
        ')YW4sFI}t7TK8TNHpleTdeTd6wT_cZsam9RLh%kd^!D^3CkraZe}2j{qcdp%lH+9u6uNqra2Lr^rtIR=<%3hLhXO*Dx?;Yn{Ue{|'
        'FG6o#O4YO)sPsx>WmKH*f0%nt(2NhN3oWs=eQOUZu9V|aM(TLK^0hYQwrsY;<73$<VE#fJsh%0Z#u&=OEW#7j*Ach_MnZFj-R}wc'
        '(Ff+CPW690g@@RYgW7E!77(4f_JA92nIm)S=>v0-Q&A*hOJ2DSI-StD`C(~e{Ip~-zIW<c2u-$iO;l1xgll?BNGzCgD|~Ai#d)Sl'
        't*3Nih)zEdRXEC$E+G>5O{A1XoaL@v&syxx{YB2ij)XfT1|#_Pze}2%LJdZTpR->G07jqw+;|MRyyihto_`=4O+--hH-S}jwCnqf'
        'e*t>8!)SNp<Vj2+#r)4ZygN>LGk1<qXS^vfjM{Z-0!7Pm=amBKq|uU!nPK`ZACSo1!C#ngG~oyN&j=~WA{yLbV(ac0RGVATd2&av'
        'JLs{N`k*@$9edn;=ghmBOItJ#IQ~rhsHUP)|5xAa*Ss9bH!dmVo1)V;BFvv~IYtGu<5+XU?nM*vYX^yU3E{uENzN-1fH5qlzpqSY'
        'D$@_U5ywS%U@z2!E79Yo&Uo6@(c@+E^b9`MP=fr$)G$i^Qm`>gF`-zy2q-s(GND-~@CYAlp}V*qs#?xd@^uHrcf{qdm2?N<yXV4y'
        '91S|(^Mi$_d;XuDo5ea1*~agBoqSS1zdqagK>3>*<{6pRavcAG{_#2+*gZZ^aiT=`?NMK&NQ566pXIjL#nzX<2eMLGIFe19mANSH'
        'za`jGvGk}pUAUwa*!=Ui$m%pb3$Um!(fr??`WjFYHYVR(Hk+Pc(T_l`slfLe5kOF^@p3ip4~z(O5t{HvK2_rQc~>}<^r{~y;WC~*'
        'dZ{7VDpW23-8e~usE1#yB;y7<DsV*fyn($9elV>NF3UB6;q51^J3=(#o_5p(Vv0++iC!1{Mg*N_auN3d-<N~8LsvBi?EBe)PbqX8'
        'DcNTZK3F|5q~-Yv&5D1o&)nH^51_npM?XVo14vR(*+WLj-Au1$wh?hq^^^T{%ZU3@j$Nkef3dJd`^{v)gx9b~%M98=J5O@GAJ?80'
        'tPE-AOkVF{DUs4UgV?kV&h*@OiuOa*CK5aP1&x1b2-$%ZMmS6DJk<Zmlwk=FXmuQ582W^bl+XHZPzd|eKByX+lKqet!A+S>{QO01'
        'E0A~s@B1CPGbVaQLK{2V$dF#WC_Jxm2MPXG1H${?Q@`XYMO^apGR2-GZE+uzVrqpxGddBo%RwdwPUI8f<dUsVB3#wC?&$)MKwB;O'
        '68oRbFIj^a*mgT>Siv(^eSb!b-WPWJ$X{x_!}R}H7g@#jcNpy34%V|UWy#Q>!j+a<z>-~qp6;;iSHqbs_j(+BKDmk<{iHG$yR9Uf'
        '!>h9=g#0F=n&H1E>$2Y3>i^cDMC87j8(b4UIno=|{}17AlhI&e%Uf#Bn$-tpZ9LC7N==A&!_jSPL8vBc4*>K<i#h?)Bp=5!AA%Tv'
        '903#d6Cwg|jKX}h`P84e0wV3=nNm)%<8JNJvv6~hj$gY4GiLa|TsvLx*1ca#&8{yr@@u7M+(_Iut2T%$2}26-D_Prq<3kbbhgMN^'
        'hyBV@=G@rAaj7FrO!l@RWY=<ct}z#1!m0HN>+)ofrZWEq>-RTpbOmY&eMXA!iI<V*4v@bNqg#C`!o+hgF{dv#H|{i7MCK2JrdmOg'
        '3lw;M$7>eL%d&4-%>VLo?{pFP*9>f~z10q?xsgZ|XK=YK<?+4m^0~pE>p8I{5=Q15Tn-pb1#F*7(wRIKr=cWCQ|RL-E8l>MG7Qcd'
        'TG;W!$!hIZ?sx69u^bz?uhQD1Ns(OXy@CLW0EC$@lO9%T40)_P(u3q#S9>F6$mP6bd44Q5K`$0640r5UDr}5pyi$TWc<6P_`*I(B'
        '*NG+$`l+VX?R<Y1!gV}DiB6!EfvR4ty1@l?c>Jka48U>ii;uM-nWT$4jW)Dx-||kUl$W=-^>$Uq?p^-DSVdgzgNm`o53}sci~?*I'
        ')E8y?7=vKH=irJ$xCBwWwq0}6<6>wND`-^+Hn9cleR6+XY%urnn!zaiAQh8o7iQ4@&I@UNqrjXWduV6rsaCx6sLB3@caj!uuL1th'
        'SC3ZNA5#3^u@<+{-<TaIt8HRlz$tII5^SHxbc!3m=#kbd=5!cB+QR_ek-2+5-XQf0p58QbU+BdSbW7Uj2DOpDe7YO;m@&U=Ps<au'
        'L(ED6eH@x9v-b;x1FHy+eQ<>r^3|T?It2}xqj%ne-hF9jH~w+kE#f7Lt*PnaC`IM}=J7NQP~@T8Wz%azZ6{aeKabt0wl7b48N4Ql'
        'UMjvi7tL~jagl(72e8G7xQlqzE{)fhyZYdxQHCx2EeAig7g<+h&SKLXR3l-_M<yC9)WBv{<K_6}PKo}9>izUqT(|I@EuGgC+8|l)'
        '4~D!Qo>u`wd!c>H(1+lX8fJY^nZsd#jxfoRQ(lTPvZfAI1c@})Ar4sx<sYU1MsHo?YIxd%xhT3>ISj8)J+-h`OwK9v!cXj&>#WgC'
        '=rC1zw#Yf>GDBSKFXK~PAA5+;Tg`gnP+v)}e;IBv+Xb;rj+IojI4X)f-J;FSoIuV0-5hhE$#=gIl3ZH;ca;i?j5lO5+s^%XQJmGM'
        'bz)BHHr;?>C9<jzr}32h!3HsYJSF{$W4!{>bQJjy0p4mRhqI)fcfw$>!pL`m{WLj<odK+IVmfEQ4@)M75UI`cj)US$4W98m<Pvq}'
        'nT4USoTNR`qwdE^^foI~OzgwNX5xFS8UcT65Jwh|#E=r(T~@V+=>L-vG6?P+F6lo)zNawJjQ_jn6A1WU^?!>#4c#iO0Ra8<|0DWD'
        '%CnMAf79`}4EVXQKZ-s87=VYDQp3rnjH;AsQUA92p5qRn9v5i-$X;B@6xDG-WI7y<kiLW)1{9}Z#<Gs!V^g|_&Nps*&|Z4?s=hc6'
        '+<Y}Y41D#iRAsF{3@1L#6Y%Kw0-*i>RASfQ2WUb{Zj8EpHS(K=V$m~A>If|COH}uYLhYUYg*K5eGe>}0^j*sr%d#rzhUBZ4e(*ck'
        'C8Ngdd@j^QMp71oj3Hp8VyE79{qUCGP)CNlpyv!MDYnCZp02nb#A8}uZHdj%0igcimyAU>ejZ-#7(3HRRw74Oo;dnXOtgXHvig$|'
        '3vuqH_Fc4=z>4lh)6bsapKa_{B8$+%CbP?>(T-eXD<e%})!(mVW>*@XOSFQcSe5_L0)O~41)b|hbFz51TfVRqDEYV;LOKWd?nJgM'
        '_87W0$uWNQ7L(M^dyO-;n_Yx{fc;gAu&*ynUmlPYNTdw<b0|n17~$v`IP%H=b25NK&vclCqVQSJ(?)w|_AsEidDgdxE*&7Rac;iK'
        '5wI7Q@;R@w02Z+2nuZi*hCBiY;X1mE<TPDbL4OMlb7we|lCk04W!HH!F7H*`B03`|zr^6k%@9naWNavAfEI6*_Tk_VzqM<1$i~5i'
        '0MC&?TwL8GnrmBw`uc(8+V!4u^H?Q_>P@I(>srkhqNVNXa_@XmYQe4;9G1BM^_~qJ-#ipGjhKeoF!ZxSMG&96ww?#HFuSLn+0eu?'
        'y2g-1@?%B_P*>Q}_Dw+|h6rj0uQ7)+3dRwf;N2~L$Tq~BM1hfS`V*j6;zOx5E(HA0(##OG?J~TIUR-7lLe~J8m2=w*@kI!7YUPL{'
        'J$G$TRDOFYMz3=V^(&fL!knp342fw~!_bDR1O9P!=youj7uliKry!9$zu7~5M9p2?nZ$S)VI%6{@`F1#3_U4EGKCSEaoTF2kC@X)'
        'hm*WkFTUYZA;R1DGenRjO-rLwBdmm2=vw)UU+n%j!v`ZfrW@iX=%H9JoE~{r3TMCSJ6&a{nZ;gP@in{D!=oj#N7{+p-Oi7Sxr!Dd'
        'yc#1k(*q^pZbn+w3Z3D$6S7;p0a>A!7%*EsHeWHW?kq4Jy$@Ul@jCz!R{-VhF0bT1s;XI5MMzL3XW>P>&2i=L^tY6mIZR`@IC#GH'
        'de*Ah<fr2x<bfFbaN2$WZkeswnFoI$G%BcWpmg0MR1TNufs(prMuR^)B01@TY6^igHCO~6Z5<=9Fq`u<u#7`GhhSW_fQ;IDd9)&)'
        'Ou*EX;mF_EV;$FJBZe@Fo5EA>>Mwu+xDSE>TLg>Qxxu_5Xz5@G3qCAcuj#;$K}W3ng5T8sPnS+-@#z|(9w3-T8al-xW{fEfJW9>c'
        '1a(D>)TSi8Pm%=0X!J@tc*pKg>NnZ%h<+u;LE-%>Gn5mb2WjTYN=8c9LKw{<#cRP3?^m4w45>3S&|ovoikw<KH63tsds#SKgpHCb'
        'kBQp<cWT2lJe_VP*OWP6d(et%>P@i&=7XJ(n*R6&Cv9<^iJ_77cN~K27aURY1w@SyKPQ%@EC#)!$bKyHB<CqTP;*E9*G+*SeStqC'
        'q%*lCWhLp=(XR$KK}Qsn^ueOOtgxKVYL*v2#Hq?CLvu1h1#ThgBrUuGbVwkSM@tlBq@ldsy7d~7r|zM)20aZUG<#%j500mU*|VUN'
        'Q~XcEA~XYHC#!|lpY?K@&DqPcgH-Tc?r+q(&0pZeI4BsQc;AR(p>=ICh;HytAQ-M>Q7O>QdpeU~bMe0khj9$3!{_^Y&7hsYb=M6<'
        'MuyUml%SfwqReWe{EQIZE&zAAZC-%nIE?xxU3}^*Qe&_>GTs0Zqe>$nrWr>KjOr6?x%>6RLMMNkrAw{y-_U8U8FQkF)fyiE<InqH'
        'i|#CLhL_4_+h_^4_w?^oL$hKWilCr*@OCAf*c@D*88SyW>KI`bYCNbx!1SB87&}c6V4<oH<#ObtuqS({@s2x{V9=rbBr(h+-~TN+'
        'u2VEAr9V(hN`Cc==RNr^7^_pHfEANgf%)q>gds-)cMb7bc=IL={yWYYmTB4SLoyXp8Nim2g8!vN>Qexpby<%pH%r^gbWQ`v`}5-S'
        'smD9}A@>-3XRy1-7kQ3XLf)ocqh}1^KlP9}B>&x1)A}dc6|+oW(+r}9dvF6BW;;V>0AxgE7!MyhnWv73SK0(Z?RRX(89kJ+Y`WAa'
        'Y<t?P`EOCJ`;zEVx)yLdnTQv?w{<vveAe4`N<thKyd5JLhv?35#>}Oaq=xUm-K7cHGe#^+M8xZXa~%3&I`RrStE>ln7y(c@|58v8'
        '!$*>@q;mkqp~RHIOHh!@s0aH;X<dK1yCUz3HF|y%tL+&GQ2zw@{E~Lz_p(aJZ}s5Zc3OakD1O9naikZO!sxgWWJ|GZH;0s<3oyJD'
        'un5#xDw7Ao&))?5I3oyNYz3E_02^b2i@M908*clu)cc6&i2u`%mteF2?%xe#hJmFRtj0T4jQsD`n`s+-TYem07^9;?u4kGD62pT2'
        'TV1d!eW1VKb4Goo*hgjIFVV|W{VJ>`*;SP$G8Fvo1!gO!d}_rO{!@u5CJ!BhOnv2vF?}-t$~Sc#Ie(bdoeYE6d~RR|#@o~aB#I~q'
        'Lm;k<I4M;oRk1;^!0%%c1awpaQ2LNn9l;ls-fejAKPK&`c}kqUc!3I!OI2x}o1$eMI-ifyCQRUsJ(wF#;4QfCI4}1+L|<h9(S)Y5'
        '0k~(Q6I57eZdfuFiCk=(Z(5krpxe}32*=4M$4c{W(HwB@prexi0F1#&l()3=RpdWZU)LX8e#>Gzo`;vL{RaHV*~ae9w7&o9Iat+K'
        'K^htGF|K@o4M#}f-faKslJ+Hiln#;Ffq-H&KAk#}!Ke3Z&6_KX;y;3b;V8_o>2MtDPbmI}n$US^8DNqILnL3S2L6-Oa`z#)lAZ<b'
        'MHrIGt%#%=++rPU@=q$In5`c?&(2S9sy5_6GA{gSN{ob&a@2H&!TLzE8&{(KCtJo$!~SI$oTU-P9!NgtzD?qL!i2<*v*CDRlXK7`'
        'Fu+<%?Azb}%)3yeSKD}CMS}$%rycFH|B|@!S!mxmyz)~m3B(j=+s!*BGmk;2?9gp;Ndz&B93Cnnd@PMjX9X(KDwi&WXA*{b&!R*`'
        'Vna!OGTd92A||A_0&7_8`>LhlydN%t=u^OhDZSN1@cl>;PaxUR1~Lp0iVFo|?2bf&1XJ}XZ?ysGto6I?_s_PNv>H1BJdXd+R<K!U'
        'mt;~o#E0><`jU8)uB!G*WcZ5U6Tny5r4*o%@TY79UbUPQSuboboix0E>4Q#?KW~6Rt=^vLXGqJGh7H#563}dM*TDz$+S<4%A=`kh'
        '8$tqO_cuL_#s`-O-g$K5h~T1YW0jwQ=jZf0nO0601WbU>B&a2rVmSU$<^cI*M69<XOC}?;CALL+iO`LQK8*at#rStb5kHw|)6AlM'
        ';T8+tBpCENce=ld2mpz}kv9WcAQ)^3H#?U6?u&Izm{V1qiD&rtT2aNc`4DRpLq-951;rxd^U&N`oB^mV=cX2oRX!Uhl!Kz)?%UJ>'
        'c1UspWjHj3)5+tr?wg3TLGZLe!##J&`J0(vPmuXCSao!JjpGs4*@&5(1M43$xarwsS&*>nGf@xSr%k>ih$2TV(Z^gdSzHrehf!wr'
        'mZ|~fLeX!e;YoeuL}#P*n18|3VFe_LrY%qwf9E)^6T&ii#_96vX@5B*M+@o!yg&DfdB0xXxl#VjXNB?~x20@JWk8NXe6LYWm+v5Q'
        '#jtqBBx1XaY6JwfVFo=AgIm(Pw+jj1Y8A|y4Aq!-iUIgkbJ^&^j01R0y$ImYfG>l$mEUGZnQ`By{YN?>#cYv8Jk}ChUs~z(mUe3_'
        '<VCjtw2j%w6NFR%ii9huY<ZOs+Jw_%ZYzQat|1NMUcFNX&z#YPE#Zb?d7um8aU^WL1M?u@COsPODVQxy^PkLhB9y!pz3Q|VL%T3O'
        'aN-Fq$Oas)*n{f2R|x4q*RlamGtyJMl5Zh4W@iOe$)e0o9K1t(@9lBw;P;;dC`kVu4S`F3999`vW$2c2x9%<#)3WCGJCMD*pNPY1'
        'z^prlZS`|RMtvuLcrs2F*BOr_IiMC6YsgoP9ZK-MDZ|R1)4bkHo%fdJL9^^1f6GC6<jsQdh3R)JzGsH4^`j#VGR=1SG6?KdO<sE7'
        'i_M+K@~uN3c_oiW9Q0pCCsoX|;O$evokxP*xpSTMsDfEpMGb4~WMZVjZjDvCgfF=3!3h4h+H%l+;)C9;`r{-nChyxvTl?89iw}CE'
        'kz|qHFY<i#6igJ_0QEG8G2Sa@k}<ff7kzXS;x^KX<Hvk4(SSMgGEO3Ljbe)dhtj2ieD4@>w3fISQrb5kKYJ#!$Kk35%sBYSWi==@'
        'lHo#twGoWZfD;aZ1JC4-Yz_*!1lB2{GNQ+v%ohqJP&|@Yoc&7H2RVKV7d-5pkf#m$6f4b9DyKX=m``J0mRgAn^37*e7-0PmESiA_'
        '$)*jxCwYN0N?xene>u)OTCann|7S_@-cFD#mkkx%lIdqu^<_s_UumlMv(}b?PFW{s36y*FFF+n3jkgSEIsju=^hYmIByXqpn;4vs'
        'r=2^s<yXAQ#I&$CvgIDQ`@UAl>x7Th3*t}NJV|3WT$kn7VGRbqgx5FqzhqiZd9FBKUn536Y_apK4d{&`2hXH&hsQCWKw3h*1-<FW'
        'MKM035=c_ZK)K$c0MMHF%4?iNEv5#V3CNf?)Osi7Czm#!SmBJkn<1VaXhRq2);Us9Erw*d2^Z;=HM`{b3#?B?$$#%PF?BDXO~KR>'
        '4_f^N>-6`WoF8$BnB4fL&Un0ycK~B`T8v-;P@Pn&-$ZD3$@w?EB2?Z7cEahBiqe#kVAHG6^i3K88c_)#z_~2clZ8uIkUTpOVBq!k'
        'ez)MIfy7S#26qQG^@8EtwpkxSYyWbtG}~F5{A8ZHn>Rnp{`%jgk-s5rpC(ySHJrxrAJn2{VbXt*?e2-qyaf?vT|teDBrv5tOdPT7'
        '8<uX>*J)Ofz_KH~+m%o%sb{A!KNASUHxC0~+}f;saQ_Vkb<2_W9zFrDA_ZvErjSItSpA+Q5f`xh?pb1!MPc6>k{I!%)j!i43&c;g'
        'sZ?#v{HX)jB=xJbl={h^G;XG0KOHC&nk@A6der%&?cld{P;oCzwn^XK9?!|>yCAwPq9-n1Te{L(ptib6S|0J#AD^686Hs1TiLC2j'
        'e^t{fiRmfJwyeVwO?TQ^LQm@@L@&lJT<zea-3b0Bb%|waz&*Pm!t7yZw+O4(>nDK=zE2RWEo^t;wgr6X*PIVQ%XDFWyt!|wt`X&F'
        '3{HXYRYUX;+LO^d;-Ev<Jg}+{yKlHmOo2o%O^Yi*@}v+04HoCEGlPIyw-R*8)hp~;nU@YU$@db2B;beA%k$R=={P&yWz396?=Qk}'
        '-k815>FMNIy|VVX8G9~IF)b|gH2Az$Y<|-6Rcz~Y67G4egh7dBJD``SHdYpcXOt&pE6y#JT1=_+93Wwm`yTD1n9OJCQ|PD&Cd5<U'
        '==h`dtg{|X@<2>M{?9M_-*Ni19+N#{CKfNaqz6D98M2j8>Qo|(D#qB|qdH^D_5HxI9B+ZKR|Gb05lqb}ZiDe}%i#rF*#e(A!wI-S'
        'hYy3F$>8RjMbiF5!k&6M&liY`BGh~ek`+Wm8f6e7=<~%17W4pSrHZ-Ul+JMkXTXt39g4tr7Dn=(Mv(<Ur3M~pWc;a+5<wITqKBty'
        'CW$a&O&;#<KEAA5=<q&#zF$AWuXB*d>sQkmpcr<b8kJ_A>c{XeOj9pJ$$E<8yN9g+S#t1XJ_>UIl3Ga8sZ`)c1%rH;+g<d>$_C^L'
        'J1~|waOf+s-(Pq+;H*Y)N+cLDWi3<|C>LJTG)s?Dmz1M{q!NNnPpoX6w0&WV@N0f;^ZZI~!8boFBP=s*OD&VP^_BE->lqTkJgbJG'
        '<NyHYB9Zg}`22gKd8AfTSwFEM!b|`A7bHAM#TtSl@3Cj6w2Kp6F7do?GU9tB8&LW9Z~zu`wV6u0eCJ;n+vG8vGP=%?<^bQV;?|d='
        '@!ko)Tz|mLZua>gQRG41V1dyPn7WGbnNqGeX=cY%tzZCwb4SbherN5r0-n+>oBe{y(IK4I!8Nhe1<RtJv2Pe&3n%m8Ss1NeBmfq_'
        '9%xltx`O|i@EbvL<D4NyKAQoZ)qO4QC*v|x0r=4r50~$gbNs+z;+f)?E^X9Jez;Tuq)1Q_YJUaJbc$8oAyj^-DRT%Cm0OqbOPyZH'
        '?Z$J_l+N;mXOd$zB)LKJ96*afioWK0%?d235ipuJuzx#c)BAddy-0|pek9K>gIIn+W5j-1zw0OY>Q~lF{34X4q9C}^*Bm~mdAB`='
        'ZpmK3M9=t9^<e#jS3iwTN~#Gk$@HHEj~qiZQPa`^Ic-c2Uoe58&;XIHDh%r@T{ehfg!(^!m5UuwoTPyzIK?H&{}_7**-?ec-lEN#'
        '(V=D1=kjAwY0WG=XM_o9_}Gz+_JHJ_sDB^aGrCiZ`p_V3d_y#Dw8KPDi%Cu~Lr-&DIJ^{Zd_3v}?&&h4=~7)I^<t^fjrUgxwHnO-'
        'JAZuF7ISA+F{VG>*8#9A6a)uGGhsV`(3MbM9EY*|o#w_O!l-4Y^7c#_J|ns{(40X{)_Kiyu()gBpZMfuE$VK1{PW8ZSpMbpkPV_1'
        ';l#!F9Q5Mr$)(ZqfD-HL8g)Q3^hB`ks=?&l(23bc5FH~QNa)xS7P`-DSg`fI-fE-n`#RMz<DWUbnC|_$=9`}<WARbh$(=r0)EbK)'
        '^FA*;=WN^Hb;dqf+a`wax~!m56zEhH%SPRxrs}_@hyEhLv7B4}X2zZfco0fe!>Nb^A4W4qJM)l4kd7hp%|{Z{NbAH)wu03_qDs?)'
        ';ctP=sXlT^n(k?{p$0lV+3+;isYe{wQAtv)=~2Cj)fA;RP{w`9Vx><7#r>qzlnPHtLvU)1m7+Fe=>f~Zw9!K0RHVWxdpXj~XWxlt'
        'wUCA%qN;&+CzA4y!J1#tiaM*1U<7S`B)S9)31J#4gCez5zYC9KRPkKEbLo*=f68NHxV~s{EgfN<l|@Qv!jUMX5cYdyuZj)^L{~jZ'
        'Tayt?y<iK+#sMci<j;0c{DrxR-&gN8M(IYp*-{%uaD5jy<vg>^%W1xJ(JI?>esP)3K^!7ObZ-SIOO@ji?0DIh3I%Eqfwjab{p;X%'
        '5y-C*5L+@&>qeMQ?Z@i}YotxJ4*r2owKGkTCKVZA7mfm?t^v>j63o1Oe~gXdq2V*c=^wef{9SLeqz7U)_#W#(xr(b1;q0bSRYMVi'
        'ph-4bdB=!Da|OqC>_VpSeS*kgRi~dI<QLdTzVdy{j_~7*-9ef3oNVAS8BH$Hwp~R2c!t=EJe0)0%IRZA8yZV!s00wxc2jlytFqgt'
        'x3R6rWke*3tWs)HxDu{_Z0koTue5wPRnk;Qy)LNdR?c*l9U77iEp7@XRF*+0(=au90IC|Im*2ywaD4j0;t`Uw=f`>olfbV=!z4Be'
        'JH<*$WC(lql2#IBf;J64>sTm2!g4G7(A60jfmJ@Bb~+%V(K<)jDb}cm2m<e4&8b-ret3QT2#>apCO%5y`}G2T<%e`V)Mb3bX6jxj'
        '0wXJSR1#suI0<Ut(gS3RDda)^e#4L9-Q9|is;9)Pz$eoBE%r%Mbb$`FjV&i#GsI3$#^gL61hW5TGE;og@(OvPzeJrb++5v%&pc{5'
        'q-hL~fJcQ_IN059APqY4CSt6`Gxzy~Z5Fk>lQXXXiuZtS93v9}NZk4I{!{$tnMTZ&r=j}~bBdz#GR*e{3tPsj+>$B)T$PCgPy~!4'
        '^&4m+2j=ovwSXn<=UlfLWBQLe)~=H=1x$FO8}0~kT1BnwK~xIAZa>lo`yAWa^&^s5SdI7=K5WQ6E_B#Hq(V5$G?kVEl1gIa74C@Q'
        '79hPp)Xtu4pQ;skzNBN-q=EDNf!}gpHKz><AeEsSR4%$}7B6yRFnff+guQm<ka>l04e*`9C5vV)5CPK_dEe`t_<nPAG8;kWFJk#i'
        '#99jXL<G;l&VCiWdACk|dS4&MbEbqlDksvir*Pr}o&ja_f0gsw%O2=pmleb9M_TB$G<04vm@9lmVfNhRGyZ*9i(qoW3)a6det=5a'
        'FGG}{a7q0SdeHYouKwJ4$Z2Q=ak9YK<DcolXbphnF1B>rpEfZ%X+l{s%zqCqy%8I}!1dUNntz{(*lTIL41oPbp+?}Y*rMv>o-yJT'
        '{e5V$i~bz#yR?c1xOW|19m;cXQX6B8uF>{~iyMzZ^X=ZUxVDhf7MoqQ<9mHV7qcKkdtvZH{!LHRH%7^=((gR1j#GL`)+;<x(vlSz'
        'Z@u;NVa0w|)-bssz9|$gs}@9xd*nfXCk76db`PF;7d8Ej1DS8`^isz%W28M{K)XIsG<_2VeoX`jI)gTzQVe56qUb$ygwP;<_8)hR'
        'G3;9_n*K6W6g~l!jx-=n83li-ck1iiEioN;soJsv4oh3Lo(NGySbFoqOjL9U*h-K%^C3Krri+7R*&;`FSl1wkYIXYIi(bb?kjqTI'
        'nNVg7^Jp{@I|2TyS>%V~=iAPnm^k=+#^8!>Uw_t4wRhqB%(1wwxSYJB;ZIUO$#SM;)Fro%9E4S<jXq6^Pfn#pII8&F+E`}@fO7M='
        'dUA8ZnQjc8=uuAf7J&upz?Zv^+vhe!Zy_uAY&CD_!dIf6E3(C4Apnj6uz=vv_gflZdx3UE3#DZtd{^NaE-xZGOO2-E8HBuOL;Co3'
        '?%(_FdB31~78AD77m;=0@U@kk1<Rj%#|N->^3{&fkIEO<^W_R`@X#sxuUkFFZPWRa?(@wgv(tUi5eFgU*`RWwFej{OtXb&*;ZEdQ'
        '6<;NrAPs>vJr)GZ1ZHv7$k4`B0j_C&_(&C3_SS@AmcAW+F&4FzP1-B}+`;>=f=Lcr80r+XJE7%9r0YDXmq$3kD)1?wWg469uWuqG'
        'Ozsp9-<+8!sJPn?tf^n#{eO=#9n0Cx#uXX^Cffnt7xw==HqD~ak}4RTN1cJ}yRn0V5DCAv)e)K#4)Du1&@u3d<kx+t&k4q87SG?#'
        '^;6{zDT<Cw?;}e2&5)Ij7K{j{nKyV<njk78dy=9_<UFy#?^jVW5EZUi-6Tcp0m0OTtK3_|eAvRx@e#DDFB(%VT#fUNtlJSW!K4FF'
        '!CDxcIdzj%J5ls!PtI?jj|UHwjDQ;^5m`T*&SW}JM-}*f6e!fS!!Pu>=u}zfN%5ckI2V6R3FCQch?L+BEPc^&W45L0J<N$j=ljOv'
        'VJ|OU-?}lU_fKuw2?~WLvnFGVmUCXQ6(Aecd8Jg3{cZW$P!TwbK!Dojv$~(MrFRTT&>}L8va9G^@bs==N4QDr2lB&#QvH<%o~Uc2'
        'IyCM4XJg)-{3r?y5E5__Q-k58Vatqx4ard5HZ4BzH@&mpG}6rCUbALmzn>0iNV?*8b8RE31IBc+QGs(RK+Ml*^zS#))^vaZH`m8o'
        '_-Ww|;PZ8?%c178>t?>e)!{P_)tMubs-HEts6;qPB!aVw(h&K}D<AI)MisSAxk-#KW|I4ThXFeOv4IM4|8!~EpTKYX=%dVtjj{lw'
        '3*HGx%-IAT5k-MVv3|6Bub0gI;LtPUw5m#PT&%<!{n-e<sNUzD`fAYZU-x}{wyOPJ&>{VaWkhr!vPGCKATX65-iyXMPJYs4=1Clx'
        'me1H{7uEk{!-~&OT3ASyiW#qp-6fin-|aQsMe4+`rgDb%V|fa#{I&Wez**=!@Q|z<6y!T)_P7=#9N|a4QrQV`UnWiF#+)gpB<RU2'
        'h+mLGYFv94?>n7mm9Z?v5pKV?PTjd*!!C03*<&cP{m5-s^#kri1A?FK@z9x`OgO`*yNOl#`tZSxlW{DHvMZC1ijI#weCV*uuBYll'
        'R%inQ=C#QQ_j|a*QlB0goBQ`3w&U1mZ>q4}nh5)Q8L++v2kp-Z`JL0%f#JdS@8P{_QjLK`EOvqXA`k!5-8pqv^1a`F$F{z)ZFOwh'
        'w$-ui?ARUKMkk%5V|8rX=1xbu!+(Bv=QW(VtGgOiYt&flnR9+-=WknCS?s^U0n8G0xm_)o@}+8Od7__%0Z~CLOOvxaOiBmovw6|Z'
        'Z@1zB_ffBV-d7MR)G}qz2UDYP+kS?bSy3w3UfKSO&v9x=u*~Tq??SPw@0ezr!lfOMq)rtWF_8+>9A@@HE=%Hj!{z<#aj!RHh8pI6'
        '$8@3|=Gc(f&t1t$ouVZwUH36zu3-#5BtUe7xn3K}$onX{t8WaNIv*<y%XlYNm<AD;`FS!=*J1#5fXEoru!iwamBoiX*j@N!IzOCH'
        '_?aN!y4zP)j@0M~ycp)37fLCtldtb2cx&VvT80Sv;eMuTWBT#to9w$AApEoEQvKAK)@C1uwmJ~Xz6f>n;ndxr*mw>0mm}WN%(iNm'
        '7nHy=M6Ndknh46<#kajh5!fKFTG#Kx)<NACH7HfkFR11At(?%G`H*j^B_;X*+9v|MRDs-eR(-@T<iekp+$;oMqf^ci1>SBN*7!pe'
        '^PbgcQY$=3v%VCQOe}=YR7$*q4A#8Rdu_{BC>1(Num#En?R9RV>qjhd&~xNsv*kENbVCX~#G-|+Iit#c?w22%!Ofo>*OG!nX-=AC'
        '%E!I*VK=%oS^0Ri9ZJPHp_Bgy_I*SvZjDQW<#(#kJ#V=&THC^92Tx%t$r$;(^C(j-8BZL*F(UethoXkOm5h?T9u!*|!jR4d>X0xk'
        'UrmQdrFmI*(h#Zuw1bE*qA~xhl#Nc+<{*lcPR2Krl}&WUtOTk4`D`TDD<U|apbN`V=&?KAh^4cj(_(jW@i!(u;^*I<`eGAi82K6B'
        '`P<3u&7@Do&yNBz+A>X+WGaHe-YS(4go~tA%!#~g8jo}*H9t1~=`7#iENcM>zn#h7%ncPc9Qvt0OcdvNkO_-$Jb$L=h3;0^T-x`g'
        '%zUJe)Wu3oWpQJD{)^FYc_6~e=Y!DkW@<pDB$d{sdn%LztYb@jn@%p5(;C%H|ClkI^vmS*IDyHSc5xY^RT&A3bSFnz;?7K<qeu-8'
        'XRWlp{CldVx9t&7gDGfE(q!v(3tNaCJuS2v{9g)4LuJv182YmSf^I{H5tSCFhrn@KmCBx%X)tC{Nm?C8;&8c#y1t%2DH6)YK8Knw'
        '`Lv=<Y~90H^8WGoWe9z6KHNBgYe|Ov!xF3aV@Km|LP}1E5RK}svqGA|Qjjc7u3v$ssq$>EfElq}kRG&4D8BVu&&(eWc<K8h@L=>Z'
        'EqchP4LLgH>AL)?5?U&7uwd`lA>V%_`8Mw`80@RGA;WP1B<48}xR);yw$~ylRBj?AgAsj%*bc)vhPk!?HI9q^QZG?LseiRN>yI}F'
        'NkH&qhnm`4Jk#Ibn#Mt%#rJE~9)%uERqqXKYN3aXkLN;(hVzv}e=rM?KGRerF&wI}+96?*cAV*UzYfD5x%u)!b$}z)W!1sF+5J%~'
        '=<OdCa856yBv(6siM+h&J)+bF{%|5B;?tX+bW6D0+_}5lxN<fHWB})$)v(hev_Asm?mUhm4P88lS$fWXfG1Qwpl^~nlTZYhWS`G#'
        'tMzy}uS5@J&xT|{)rR2dO2|qQJS*b`F7fzo)TYm%qK;ZxoGet?bAT`G=cwM8P1UX#_xr^Vv?t(+T0*#p?aD^;KId~7Hos7QF^r!M'
        'J7i@w9d&B~Gg|93Fiv3SyB<^Qcnv^yP7YkC^lfkAW$2+ZSd-*400YQu5F0Gy&o16)YQBlntHSB??azFU`(3?AcSzyqE$r2LTPcu+'
        'ktV^k;@t!Y_~y9%^$+uh-pvt!yXAMM7k=M}%*sZX*Gk4A4%7^~!Hey9_|*bB1!f-H1_wh63Uq@^iEsm!s)v#=7w`TJD(4e%9rdHp'
        '(^0_cc`|_tR-cvE79$NX?Qe}j#vqK-<a~;<g~|3+Bm^Z-)KHK_4jTGmFI}dVqRP#8W+u04g;z>;yb~%Lq<D8QLS2m@&q!LPr&0Mn'
        '*CfzjE?V71`h}kJf1V*VZ~`2rX$UFC>?){wMck`TUP=YQDbpV`mashGtcFd<?u0n8noa6vI#dDct#N&h2(BwQ-iNs&uSic<Td)PL'
        'o;0K?zNQ703fk4h)IFGSDIZLS<|dcg0hg!Vd%sNj2%nu$lczP6U*=%~ETwoMnWra{oxX<=45;@TmiI4@<NJ)r*;Pc4k5du@Q^SmZ'
        'L4ubIN>caf(%5E>=&zukwxM|K4j-xZ@hqPz$v-mc0{4^l6^h9CHo78)x|c5wNP7~#RN0UtY3@ey21@Nk+z{Nqbd!yQ6_~0a-(F_U'
        'PfcJ)`j`e)`HM@2fpX`clwz~d=)twQk%iNOh>EZqx`YU06L@@~SSq^lJbfMvv613Zja>!*7EwPyk{F+Vw*{JPHox(D$|YlP&(+n5'
        '#4tMcQ2B`OlO}y1<}<nN_aqg;!DCXncnfpO0Q6+C!sq=iL$&F#$9L*{batUDmPQd#>`bG*OZY;dP<;-P*_$jHsSyh{Swes!y#by_'
        '(~NdHBnFVggz^89uyw`7U{XN4`DKsmN9aM2Mh0T4DBKu%p{vxv)x!Tuhjb@_z3TG)8rFjul;jZj8Zbq2Vk>Rwze1N8a~7+RRMPq@'
        '`}4IZ;LEVkg|e~&Qb|KOc%WunkGn1A&dd8Hf`^g&)HBCvKsSp?q7v#wj5@+ytlwTgKg6TGjXmL>uGU~Hg;9!Qw4zuN(oa<eFPE&E'
        'oKL;p`flG~)@KW%IMGy!YRM)TAcR(H&~HUNAwK-K578+qeX*9{7s`U=9?S-W>#&KFT^O<o)v6M2=;vGJrBRG0w-4TsPM{a(b3Sw8'
        '8y;5&Mb_gL#8xTsGeZ%+sPKJtEdS>9Oy30dZVL4#{rO8q=aDF_APKNwOp$-$o)>!U)C!HvCaYFQ>xE~q#KDAF5Rag(GB=sG^pP^J'
        '9fg`AqfqoaS1^y%YoNi<k_=-YifmIHzh<?#U>LX8L2#h+_q4N~X;T|Ut#rqs89#ep?R4TO2hw1EU>F0FT{!4HTAfDWKCc@)u77GY'
        '7loGoR}(ae_E!zu+h2>jdU#}kdvN1V3Nu*&)DkBK!XUYT%D;Bb8_Ubr>8$?GXC?F)whoL<C#?N{oar0S!jN^v#XViXBJvV~XZXO~'
        'BO>EDR5AMO8(!=-BumalamYsTtlc`{k8qikhk`K8Ud@I5sr5LrH)4l>ppkjHP>ff=LAsRd#=qSM!&?^K8@%6_fl>Z3v#zVJOy1o*'
        'X`nc=^q+f3qj;$I)hH`R0sNggn)0ok^Z^z(?C3e!)<?(!RJwEwdHz?5Q;zaQ=*c@w?uN0qw1S4Nm)3%BphcI9gX~~XFZ`YKXvsAF'
        'I-O#2mBJTyJooM&Vd1ir=y9IAhkP&K0u#}u*D^7&QBBslBU+*S^WfW_pL2~dF0#+9N8Dd3mr;?Bgxpfqn2Yy+OXZ?Y{KK<oQs5AW'
        'mA5o^pGQ4Z*&+@8wN2{B3>_r$ZFh8#ia39!8<jBBO%T-hu*Ci!5uZL~5)Oq3GYd(P1*;xrW)4H&8-w4cf%4#9_;+)<SnIS0eL6%v'
        'P`L(n9$sWAqbd#i*p$j7x=6k+Hc541xy@$^&vQ{!1$lD)XtT^r_)wKs(Pt@{yHLKRkB>og@aJNwOB&5PH?D0@a{)68@d|=mF&YlG'
        'ryGV8zGVaayk5vz(KPTZ736+2?cw_FNO;#p8&H<k4QyzB`0fd{f=)}shm>8xBuOW|=0jbArZ7G1bc2$|Q|$ZJfnZhFQ_5o}(=B;c'
        '1-H_RxyFSfn`Xslr;~B~7b@POiScO7BzbL{rKy&AxZw%C{+j&B6Pga%jyC(v_3nPP+oMe0bhvfzVUX9KNA2UMs=8ZJ?JGojTjtHm'
        'TH5Qt+|`c^NWw)8m&YrdoWww`%re>*QL_zAJX+?OaK!O9)_WmL^nzjTc9GEc*;~McVE;25d5<haHB<5wS?-aoBF9oCF98<vqg+($'
        '<Xp1cV70@NmUjZa$|;hff?AvDzjVL+P(p@0kLb96iyS^RJxXbJH5mF3Dj?(rWyR4<#7%NIf-tV2D%`!D$NpU&$VOf3*w89@Pz+v('
        '^BeA320&fSeN7HulLWf6Aa^(pR`ST6@{g>UDi89U<}J159Wmg8^~Cm%4+Eg*X7%4h?Qd1I*>DPL?nQXX+^B7YvowRVNgT&O?-|4^'
        'S?&<vAGKdk3kY@UW_}jLxGLoB@Y7XRD<7ysJ1n8J-bXGE3)x@e{irlYQ45+FeEcXoy#|xYGF}dZC_Vfr%LOO}>pg1qF`5o#xCRBb'
        'C12!o=+&NYieY9~?Fq6HU;MYZT{zW?CPiPO1b1A>Stn9gsr+gzbg-Hm><$4@KZg{8CRB(r-7S>E4Ioiu0StQ+gx&E%^CiqIvXvpR'
        'qE?;#+I+4p%n%>UE#5UDxrgmt^M^_#mz`^&=+8%SzpKSpkUj=qPA16+1|cNzPe18odBE#)TM>sE2zzk}o>B`r`1MF7^{vj9{P#PS'
        'XGpI~V`iKU3-H@)LJDOXv*hpjMF@<It}tx^oV4qCRI!0MtNY#Zk>cCWCeOWUiZ)0RU)+xC&#8pp7wt6Xz)o1Dn;*xzA)$8|HYIv6'
        'Yyd8#AL?VScsf%;xO_atLqVn50E`nZs$Y94Z#o<0G!IsihW2F{hBlo~l}ALAnOixX&%AeSxy;cS@06t?OA4Wp38k}2aoK6BR26$B'
        'k^;u1R2%&aXwuWfdjOH%bxo)R{m2)t)S(iS)g3(YrLla{U+Sb<T`hgccbnC~{Wf}}5$h*bi0s?eJVM2ShpTPJfB5t_Rwxg5pxkMA'
        'zwh+J#J{DH4<E&%c(0^mc$s7$%$UdZY%LO5>Igu>bAQfyLoxn<>0}j|{Y-I0Vdg;cvN4XvusSnBG&y#oZOtR9|K`jU`U?MuK#&y<'
        '>WT_{<xYj1h(CIqNI3YJc>8fOQp1!NRNa^d8A3u4gDj&RteO9ebe@+Yzy5$b-dMHY(tq48`GC-D{btR!qZ;6S;wG4DFRG3zUZGz4'
        'Y|k;o5F-EYgJGC*-41+LXVtl8BUrK17IvkzAxsh-$&)w}>oh{P+++6>eHzR38}ZC?KySrp63rboN#Mm~HlY!GYatGq`1cCXMIDaI'
        'IAWG9)~OWiB{UOM{#f0Ay+WtdYxy;BXwrC3j7YHG`Ov)t0B|MJ7dJ)=?~SRmuvs{E!R%=nPjF}I`(yY;`8KYBa%kh$igFIJQHrZn'
        'FI|+XolL<)H?sJN^WW=gcdVidr+E?aZZ;!yT5m<~$o69Mn3@oa6nlev;9VRR$#ps3U7B3J69)^Ko^5p@sS~Yd^7Va6#6qt{AR>#H'
        'UM=6=ZeL!r7d@F?_lnbc!Ywq4&fvknvy7RO7)7G{BO<=iU2-)xvC=lv(l+zZeueAFI#ETBHq?-z|B}zi3&?g<B-<euWj?7F4JjNF'
        'x+e~x<kxEz4KziY;fR{R+9AsE6?nbj5Ibuw2mL6r0&-sMcEK5J!r<~iEZx;^nR$#jV4RWJ!|PMViJqR^P7c7^@07)*f$WQ~FCwOh'
        'Lzp}$NM&1bLZ3>+4b+NDWk0n$hw=Upsh&ZL_r*iT+)Br|nFT7wvPay|Fa>P!TVXgk>4&sqKj%ExwK&`UXXXG>V+g^8@wyRPSfxEU'
        'F-7*I=ntMUjOxlc36E*U`*50fv;7*{Z;>2CpOa|L?c$3%cv3vSZKpxr{=PBe{nvvYsk{xVsH3GIj~kl`IGE|jrlc*5oViTU3;Wt}'
        'jM|%04I{wCpLM)$0b^;2PqPz&rVNpD`yFmG%|6pB{%q6?xL@u*a?uG4ha<C3Ro0}gT)d`w%YF#8V7C=okYJ1`o~ZHJbPRuh|BXUC'
        '<@atyH7Vu}qUa~oy5Oh<`$<mi)cBX)VLKpgQx!{6ph0_4y{e>4%7T|fgyuK-tzp9ue@x}ni2(dOel%qZOl1iIWg|qvG-wst0Zch1'
        'L~$bMSPhQ8>Q74dUjH<lwzK;?!~uLpvi{HI*^yamCVZTszhW@BN{dTJ>{6|>sL6@4%D(By7~tVJ)H)N9E5dIa)6Hh#e98U6_D}La'
        '_I=)B(3Wahw`v7evEpw?IUW1wJ6^8V{|x?n{Z>pB#Ou#jFs`_`bAFfgZejb$kLI!&C1lI^-^)$!Pbrzo;r42~7}lP5DiFQ<9$oPd'
        'I5p491ct}xFWVg~={8A_SZ>|fG0(CmjfjuskHA9+tiTTYiT$-HpcttaTi9a!Fg+eol{@tXaVRXUWMFH#UP#0tIg`5Wso#4p3ZbSJ'
        'Hb5;<jXl|ab-FKO@UtOJme+~@oPRuG$A@D7ujWGWMGD)#EIwlx0ZhFG-R|eWm4{*NP|!Xv--llj4Ey>1K!O1%sXpIDsPa1tk=5hz'
        'U&vD?mBXKLi9ou)bWC#j0Wv4?tN0OL;25zxCe8nrA~2&hBHH<uv}~j39lzPc<DvYAAPjXO3nC8n(=oG;vXBlBKSya;zc8wSlw5d}'
        '5`H#&dVJolum2kz)W{$VFo#^gsO#Gbh|%|)36@(GLMpc9`a4W8u+Bv18Gq@k$sU~y9CYm%IxxH;fQ&JoadrLEeK>bwC2e%gqY1Oe'
        'YmE=O@4FeE7{Zg$Tz&6T;0!EQ9q#K56837(j|lGCHwKjf+@qH?Pz$3a;m7iZUw7Ro5{-TWNZ;p);Wxd)D%FQ}c6Ep5fO2nxxnU<o'
        'D2ziaErR`VfV`;yWtv{`1ua`L|H9bLK@|97X?iP0I9Az)4!3S2<-O0H_nT1?LKMomsuI8ksc2q^ciR?JNSm=LZJ$}J=>;<Ehc8by'
        'Z$`8PuRD=6^)+`1w5`kh#%Hr7*2e-HPtJR-$M=Pzg5ld6Ure+@U%JF>yR-dQ+{>&3G4t5JuFD3QMJ&e$xvL`ZN1OX)SR*cp2T#YQ'
        '7qE&SJw*Y21tY-S4-Uii04ffI$fzTHFbK_gG<4<KWx*~QA>bRoC}fOsLI0=Eqb|PJffNL4ORow?`DHsNeH2?aKDthQu-R-6<JU>)'
        'nwYmZFJwDIbPAE^*XN3En__cD#`_YMZcoX-XIU5%Pt2G&7$j)mVUxMz9FJO6bzGNj#%|I{U@j<aT_zk@!XO_4ok5Ga)^0)DdoMD3'
        'AhH{XoppmW_qpeHkijK@>w27vI<yeEc=y9ZBwA^<+ccU&5EU3}8BIr_4b5LFCmKPHT@I3u`WJ`0=kCmhLF%Uk1-UC@lR3DVs9?Hs'
        'syr&t*okFOu!~5WyVM&V6Jp(JO0;N&f%nfbZ6;fgVvf3kV!u&^y(|w>5@h^zjrPg$fjA#t%IEf=w%z9nPRPOWF}!U+760;sfAtTI'
        '8KE>^bhF+^_-DgI?n6(^LtHOtoD1hJ?79EuWbrL@kN8c2=r7>^*rYIP3tj2d(qsMSW3-54v%%;!84*e_&A#7ma&`cfwKm(Oc0cl)'
        '<A69w5^BZRkLGLnjyyou?eQkWr7qavu@b3*&$sKr*V`%w<BtC18*U+E&%8$Tx2p<6c2nl#c{F*?HI#QZI<q4OcBp^(t9Nx_vE}O&'
        '1i*cckhkC+lV-C+8s2qDJ-?<4Isja4oPevZS*A8D2C?#|O_dy?;Fr`})Nw7hjbwJFh6qb7@n!!^(U&9!%Db^EjAjW-W13xZ(m4go'
        'KkKti%M+I+@ghu7${#EU&bS?ROzoDB&dmtx6tl?n>;zyVI-Bo)4%gd1+T#&sFxK^`ChCMI*Ho>lOQARL-%8W&8;LRvD63&szeg~J'
        'yfyUjSL~1l8a)S}7dH}RLKlU0LE$tVRZ-rZPHLMOcSZ-O7ElPnPS4pTh5ECg9%0`E2oH1SdYhl_e7hRYp66ghn#x&;7{Z+ztd29}'
        'D(ACACnK#2!rqJW+Zy0}#oh^eeDC-ipzZ%TV=F%mL~&D;KQWhEi|KODWfzKtXbRVZaIqGA*V8e#7m_Ik1$iyYG=8!9X>}F8f<_Bg'
        'koG16om%Q<#Nmy?6h0lXzq=90VKN<t{wRdA&Kh-hJ<8Q(xBZ8<Itz_9+6&o4L?=1ANH6%?6>I15w#DThm_fdOgduCp9wrZ|xE{VF'
        'N_ohZ*Ov}4iEEo#;RHa0(XqM0(6cV8?s4Z!bJDFwfPaM5-5PeiuT&oN57JH&+4{%EoA6vy4;!y@&gow;9FGm$EAW~lSprop#3Fr3'
        'eG!pe6#L$_<Zlr*d^+PgMfD;6&g%{7hC@b4ax*V!`%35@Q~9n_@JQgRr{FUpB%><z^20Ptmj$BsZ7))m-ye2LH*$)iP@enqd^&jt'
        'pB2H@HknC=EWjdNQC+5szH_0M_+AU$2sq=!qrlMhN0!7ap($KZi@mrMWys1l#{q+9A*ACDeR|S2IpHZf2nMj~)rBhy>NT)NMyCYn'
        '3n`?O6OH-{V)+nV@T)mEujiJJjut}uPk_e-cYhl4YTa)_yj>8o4@D%E4$ZbVV3_`8X*p9rF6E#k;iKZ2E9S~{G=mEkha;P-*JZJ#'
        '<Km?+j9T%*tlO;4EU^`Iw@&;iD$U7?qD{daGjO0YJzJ6NLJS?5*ns*;(zD`RZTz9(iSy-m+{oUCREcFn#<!WVMZ_N=*r=Ds4^=-{'
        '6d(}`Oa>lk5k+^>bHS;wy@8^-K#7IIk>9eViL-~y&%R5#ZqFpspV_?^0w4<pWrE(hdY?$WLtU#IdkBgN)PFAAfiikj4&&Rd$22fj'
        ';)>CuWg-O&WEyjSzJ9LQ3)`x}RYKavurTFrBaQHTKItV?`;t(@^!&8I+gW~r#^!FSI`|596de(59d>+V^c`H)k_?J?$y>Hq3w>XC'
        'ws6TCyORmi&UC$dfPg#wFog>8dEYiwmK@ZJyj;PL*&3KKhhtg)w-y(N9wEgO;_i1+&p*`GHB-T#l?7dEn58M7GKtHL022{^SSqk;'
        'VHOnpxLUfwGx1Ahuh*Y+*v{n5B;Y;~3SC*e{UH$t0_PN2MjC}P`5*Ij_*3~7Rl~ah+|1j;HF)y+c~DNxkF7CFlZ%852DEQ<>FVKB'
        '=r!C+XC0gls>c^oycA*S>#o5wF@l-f`ksgfMlzad5l`CFZ-IzcXJLbjLvjQUCyr8>*Vzy80;*G;KX<ePPF~v&S{ZLfb3QL6L#cm('
        'tT9mql_P~Qi^)vH6rYz{&Qs0wh3ObZRbO}>3^qoUk~|I`cYOi3pC_cCw-e?)Jrz7`tvC-2fsYR0aelqe?M(umOMA`AN`yqFc^El7'
        'z0j-bW3K~8{5)}00kNi$IQj%Gl@D)>-$L^op6U^1mC#vJ(cuBHf5FJ(BHrOGihFGip(+8fDG9TL_?Gu)sn29-F6i$ulFO4E@bC@i'
        'F^iKr!7g1KQG>vczt)2jc0Qo7<K%v`ml60G)QuF}`H8>v3`P>|g<`tCu$XJEK_rH?0e`>ro4Rv9b4lYz0T%%;>`<XDYEEAf#~%zW'
        'mpICoBsxIiiNCh%b4PHTw0b@{Z5ZSUas6XI4^(aNE_}61XAOWb1MJ*P2Mdm$qV8T96-cGewL&uI9L5Z(<5b0#5W&H=lph};4Y0lh'
        'Wg@>DgmGYWoNr5IajzYNwI#2*aq9wNNd7g18{CuW;*8f;3w6JHZX4O|j`c)H8mjKM>U;fz5z1xd7d`=73HJt^eE!2mO@A!}CTqED'
        'z<+2=dyEi3pA|P}t9YR*?M7iRQ^@0FP7jX5>dSuhY47A6CSw)bSW2v*QkLu)f;t*xj+p<hw?3_gF31zEZ7-~6)_i)3x>MbyTOpEh'
        'U}_GPcN#1?pDz#sBwY&!-j=8DA}7mV4(IbG=-TMNV$yj)3?`*28-CTuStXKx<b~t~mrC;eWEjSa>q`F8*4M`T{4rgTycOZ7xVO?&'
        '_tMw#sL7OMl@Xf<qXxxfDpRwEUzh%f{9||Uz2&r@CIA}w>~|PI9QJlBw_=U<%^EbD)i-JvymFlI?&4CU$9Eh4n&Ze5ym2}X;LPzQ'
        'F89!Nso5<j*2c;$`ZC=nBX{wK&)H4&%OSX|qw3iL3mp~-V;x;|;qsWm7qh<Ai6&wM)GN~1_S6e~P7QRu1@e+#NjCUDkzxdL`cM!z'
        '6QKo@3TEf8$zsz3OxTaa|4`RvI)NOnk-k29EPVVc^mU?<n@Y_3PkwV0dq=BI^s{0IsL%8kmE;D0PM|$zak?Ota1DVAm<^UEUoxvv'
        'HB!b*?B)R6AmlUKkkIirIWtr(?t;L@h#-peU8l9k>oq?^`DC#r0EKSz=3|-Y-U_0S<%Xo^A2YQs0}}0Gbd!6bcB}+}jwK&2OWw;l'
        'M(_c&@EnDenOtHU%RuEyVF-<D+{@l-uHIz%D?L+{Ga(KPv%#*r*;hz-%#3$KBI%_Du2x2SG6yS*afkND3xQ1)ngsbax?y|uwLb%v'
        'Dl+z`5{wuH{3OTW9EsqU=B;oTZ)J0M{($X*za)=8R2F9Z>W-&#+;(vI$B6%Zb@v5&F!5d5M+%;2Gx{j(_`T5RZR4^{qiQ(via+G-'
        'KI!7PzsQt`foU+e89nB=41^haBMfZ?L9%Fdzum#xA*9kHD%r}AS)sX3z+Tc1770dR#w5-i9ZJwVj?*96;3}1nF;TkF!Vls11mHj#'
        'GK7w5kVX|aU?vZ%-<9NaW<HkpDIz^FI>JO`%Z<kh5*rX#bEN+)#YFs`q=4EFi?Nr3FS8F7rxV7gM(J)p{8wY4oo%L(gz@I1+3PKz'
        'j^EY)v}G(A@fbDMppYmMlCBzYs|Dr{@odh!<Yc&UbU`g*V8`QPvZ0TnFL^#ZjA$?xRy++%@j+2*IP@~yn5Qb3IKmHE&KqvagI020'
        '%)dG9{ftuJkzQIS&YlHgO3a{)|GitwbY`_oeRM3uUWw{IcMTS+RD4_{)pwD!Sb^dr?sfE}m6QVDLwq&el@I{m0FpX3L?MRd&s|$0'
        'o{NMT&yDPC9Rg#BQ*%7wlN33k(9QW1Z43u8H-x)=R2=B%Aj?t;Bynv(u%B`nZv|<|kKcXnShj=nOQ&~J4P8VjDoO*6ISEJIrSj>L'
        '3A%2bH3~ALL=k*3R5n$Ub&`Wd2*4fMiX3H=-7$XJO5yuMk&x!3pU&NVO7!{|$h(*>GMx9d8o8OW5$$dx3)tEeCQPCKZ4a@PbqCAk'
        '9is+HqW0<ICma{$%aJU!R8n4@iD$8jFX)SC&!o2}kCPc>4qtrHt_<850=5@zz6-#j;roH)$4Z9(mS*nNHyX#Mod^^akOl|*jLyB$'
        'PjY1a4x`}vYu`S``jZFd^)9L?@G7G32wopSF(0nLQM|*s>&9L(qEi1cd59}Jl*fuBZzMaVBK_AYNgC6fpp`Q{>IIJ46}C%9*d#B}'
        'U(ps1&G#STfkOh8&F;PWJts_tL45VqwWWZ$?ocyk_I7aL@sv`W_`FTLuRb<if?z6b&ZCn1YgP&3FBCUqcG$*h2;=YwOm%?`TblV6'
        'gyZjwkv}4Zd<=pJgEEA$4GAIzPomvX8Dv>8Y$lTTDe(&88;yOi2};&P_P=72q`}hx0iYZ>m(89eDf#{@+9%fsUbI@gCkIXPUez_2'
        '8Dx~<wO#HUH}UIrl_`Ug)u$!7am@lhQzwoR#W;yxWNrjMCaO0s2%KyvU<MJt!vr!nK^*o3jw3|jiU)(`E8o>5^Cx%e964#qd86IH'
        'K>Dx(xo1K3WQ3(4c08U$lz6``J6GA$FTqP4agUiPPp(7MxN2YdhU0}9r7Zft{CV6NL($ObV|-kE)$ks_s*Dext<V`Fct0NwT^={('
        'z^~tswRi(!!%YIOj&%cuDXL0#4{3|kH<Q)#B3SSmU|W9-69*J^z>UVTEaLcl^_8supd2NspVy;sSh*>KmnC1S*~_e7D=|zQV;Dn-'
        'YOrMtJAZMtpD0~ptl9ieGJG(IfzS8m1b%K?vQ)%od*2LBc>Z5N8DPYkhMHWf7j6EFl$0Rq#~_&^xCK-H_83wj$AUhlm>m`zc*v24'
        'R}>=Cj^{G2v<Zsbbsjx|DsI!Xw*F5z@B`5(MClhmGW*!NVz4$Bv6>?}|KAwipAn&xKHa0cyMr5sA3^D0XW6qyZSiz|y<$H+bSY;y'
        'Sv3(gUjTPgClk~)NbOLdlsysc$hhBm!5x&f3TPNq<Ab)a*}0N9Iqb0^3Z=v&4znB_Xjl=_b)Ti#dhh4lV{wi0--r`({3zDt5-Ntt'
        '<xCvqxgPWm28)QU4A}wuFo4S0CCs6vxKn_NnW&9-L1Vuy^MLF8#xPQbE4x{XuOs4q@J$fW6oy|E38_u!R|r0PkYy@m$=S?}<GpGu'
        'R5vXv>!j!xR8J05ijYvEr~N5dkW+$akX;JbRqwQ=!}h|pb9SdHqK^!enn|2=DdK^gq0Aa%#hM%LUC04f_}i5{2y`j1?_#mBkgD1^'
        'RMO}x`TWCeuRPCbMr0aXTjJEKZYG@{x60yG`eiH)=HIshFC5r`FIaD&DHYt}ZBBWwyxrD9y}_>(8HEHmC1R#|G39Lk__%AaqoN7i'
        'LlXh~)EUnTd*@GkNwqqg12B>Kym4o>)ro=7;bPBE_fSMXLFii#&6)Q5x0B<A)|k7&_W|8^+@s>jxgzS|a-FShvJR8r0g_xb0S<+o'
        '@}mmY{C4=tLN`dwlmJMJN&p2;1QnGwo)<-Zgf?5+48UfjvgnN{s{ub|Xas?cXNEt|L&p=H9zc9R&Kh_NB=h(=4<_a&*(<;VuO=#u'
        'ojJ7)p*fx(M|j+g-)9~+(WG@Rs-=S`NZa-ne$DPRR0_2I64N@!G;x6@sQ6}gdeP+fZ$8k4uVBX&z}&YPKfBw3ko$~Va<A0^Ze8Dy'
        '|4#uk`RkPw_o3N9Uq@Xar(qx$ae&NqG`XZXSd}2rJ$2h~MIChkokg^$o++7IUv0f$X)<%HXPgh=Zq9bC_AQc(n92Xj;6p3EP%2I9'
        'ejLRwV5oelfIGbBz1Z5%h)t5shD>;Db0c2nnCg4HbD!rXE)-WpDk8xyD_WMm6ISpGcl+gE(=_1`HXC_ZrcG$-kqV8b@r1psiU33X'
        't@7R6^r)XDa0g$5>LQQP3f)M0kfH^)`f%CEk>*68SPcL84zdMDx*BQs-M86z?+;F+S+k(5MUvZM7MPOgZW7s6?If!trpIc%FSvna'
        '3Ak5pb`eN<`*VsImS>|8Pso?QtSGQEh8;`e+RqGCSpEB-1vZ_9Z5s}8@~!seGJh%R;rOo3;^;&cMdM2oi@(B|>U1zXKhAz3lZ7Ji'
        'S81*I7(k4WcaU8T(h3wVR@{K*ci(XreSb8Zcj1iC(QqkYNUO2OZwI{^NTDWT8iDB|-yhzyA{NU^;=iGlxU&vEZJSEA77;T;3L2pm'
        '*v$p2c%ir?(*Z7iUfMDi3A~N|CrP%~emb&^IHv|Na@X*t-95D{Vkco_0VV&}3>;vFS20dD@mUyQip__BaTQQeIVOIpF6Xm&lKmLW'
        'e59L%nf7;GQ@liY+W|!ZH*_}}c*YQ4i^+Bw)ZmX~yz-mg-iCXJ#Chn3(D|3@KfKwGN?1o92Xx>&o19HB+{Y8GhY&<cl`T93?KYs&'
        'Rrv^*ov!1Z%m6@~rV{GVJD9%2fK`W&e9uSs{R<aw16hOrc8cx2|2C*R?Ilpa?mT0}Re?`5I{e-um-98TT@JE?uVcBHBbP3Pr8Q<p'
        '!Dryt;R!We<%EB7N>GnZD3qD~Xu-WK9~fYJP2#|)g6aQ#GV3bef&VRCyo4P}51D)j6Qw?6aL0=(mukzeW|!pk{+h*G3p3MG2)LQL'
        'IA458IohGGN=Qfqx3O_I+2<sYfkcJHa<j9Bc%q!wfe&-GPyE5st*`T%!s+o-H2bv%#$a%_6JlTo&#x0D4FnYKK1V89FHCKwiDKB{'
        '@}64`6WxQ1q#%pKDh6mnU71l@dT>U=MF_4hX-VHt|FB)M7ZX_GOI^H3c6PihHc5OCbq$7#ASIhmUglcc{{3MAUBX)t8vOkZu(SCK'
        'X1B(qjt^>tV@MguFNF5mCLH%{cxO2ar`eQV=5)Ets*+gyZaoeuoV$xrcs3Vgc<yiU_MIUNpMhHmF--svJgR}7|LQ;cW>(bm5yK~g'
        'KWeh97`)t-A?BQ-u}M1!1f03aL1b}H<ZGz{Hqc6Htqm;4X138rJbHeXESM<GVsnOiwU?Z)*Q9XaEI(FOa2GXI_B^xWOU&ubO+vJ@'
        'pzx$Ye+|s9Nu$?IC-P0{{pSEotggSA;aKRu4<uystqm`h)?gKFT5~{6>-W*tCWWS~JG1D27RE-%u4@|He!(-kI?9@`faFVDCOxO4'
        '!pJJ+-+5bmZ~SM^cbfqjn=<PLd=EC7_W0sa$wsdjBx}_?6aQ{$->Uhk@QASZU~7v3bZ=N%3j`O|_XR<3&5(235!KCAVTf&7tB|_C'
        '79dtq5&}5|Z-2-m=DbN+!VVkqFIBA2LCQ;>YeQ<yU8xVs(I)52haBvnmQY-xVk|J)vGvt!01>LUGHDH6rcIYb$~#0DP#Ox_>6mzB'
        'e(hs6Ly%^_|5Rh<;UKvawS|S}sz7E(2q1OOWEu_b^`|EgQb+FLD^ASQX>{hM#z}bIm~wgZ^^r3Z*=ccrf@Jtp4qa)yd%?IzeS5BH'
        '+h<VaVefI~AfEKM@_$eqSao}`%s)h$#TqvM=qd217c$qnNb`Ydj^b|nuBOK70uZArU<GUc?J9D0z;=sBwpZtb(sL|1-{Hdj+zJF0'
        'X8V~@UD$A^J>3@$-D9;z;7{mnp89wGrQ?~deq9cEy~PcMIVGB(fz`Z~R2OHQ+b?p@^#rq_a_UukWwHodC9=x>B_^pzyJ>hT_~9kC'
        'x)z<mn`lD<IxpYUlnq-y)!rC5(SjX_f^^6oJA8=RMbd!YCY2TRjOec4H-m)Wv8s`cdoA^sR2xLX#tsN>&qBs;b-)iRKsL{34cpU-'
        'm~)pz`5}qZb%%k&<Uf$Hn*}$N->DzbO1sO9y<jR+{ThCg$_qT9JBRUjl63!8NKpSzWq(y%1=57!;l-I5ltOK)Nd2edCcldCa!i|i'
        'Fxb=iZDJ@q<vy_*GJ4q1Y@45d54co)8ibZtpDa#X+Mgk@uRF7DHsxy6`Ckd#B2H^3AwOpVkxv=?NDDiRXWyft68eSr5xU3GFN$j|'
        '9#QTmeLQ5zTHbCrmEDSy5{;_e_ov{!!Ix}!;JTJ9OsLb5>t!dL>m@PTBYcR{&Au37=uEdbhg~P^ADj@nYE&xA)R~U*x{IA4C(Opp'
        'b_BeuU=kzan#~8&6=Ft-o$6_jtN6ORMk_+T@JyN=l{IsufR%9)TVfoj7IT1A4WLd(A9<vntamYINgq}+Tl7uzC$M9PBz%&m|4-=+'
        'Y2!xu^$!gA&#ZJPnH*rZTI+je&@H%y)7C?W41R}<&p++bZ}R+I71~eyz%?WbJM_kuFHxS1z%XWnX-g3i;uz*A)=y8AP5bX!JH2<h'
        'B;K>4PO;GJL6n|MD4fJK!!Y@Rk}5;}qW7;u))^^sTnel1Ud<jGWc(;vZycAyA^(&o#djei&kd`!CIJAXy)8!@yKf+W9wi|zXq`BT'
        'j2VWXUdpSEONpBZe<Eu;>M0?Vngf($J(T}#7yH$t1uo%kFIqiz{Ie*sz}nr9T8c6v1RWK4I7XocvCq<Q^oZKsXZX?oV53igdvl{;'
        '>*N((>`+$8VGkpkBXgf7lOS64N%~c947V4wvGnt#(cB44P`yOma9AMShcKkdR&i>UpYFu(F6EGa$I&wOHEE!#T#?|m^vnlje{|Z)'
        'HX>NiTKz5;9XBN!>7eQnZ#k-L7YX}x%t?ZBlE1G)X+?VZE-iuC_isuMeq4Y4&c>17PP$6v?G55e|5G&y2Wz_pu5c%2Huy40H2)66'
        '!$3R7KBoiZ7h668*_uvivnz6In3N{qb%twS_8<*x{)?IEPVxjnFS!bWR@ZOT;6YKY7+;c1SB*`W{hMy>yN$61!a@|oC04qRWxSEy'
        '{Q^7S8CDgx02_Yyt^VNV5opdcyCxW4`Kh+tlY~GnYjQ6!tFyERni1eLXkQtHeS_#H{aWrl#%@nis|vk_b2(Xz)%Bsa#B24?BaE6w'
        'zgWg0In_csTlH?Ga;-55rUo?c-Z@Bas+9M9a1MrkGlS(1)m+B@VGDFmT%{<7Ti>~7Jn)3t33t}9#A)X=zXV9r^rgA=tFsWC5{G##'
        'cpxx<A)Z>>GSJOX1E{}`C5pfy4H5~Tp6oI4BHBswp_dEb_v`8JyI+T{lK+;s;hDqFV7Y|-S8p7~&wtZs@t%W1e-J=5u&S}>lQ19)'
        'vVbXK4iR-BCyt1f)9a3L|DCh#eQoh_>9H_9P2`YCUld8RiWy4!&X<TpnQW}o-a1T#GY?bu<0>)NS!<N^(}s)0t$GM-wKs(%Mb{xQ'
        '>^JOv`X9~G-3i8ZT4|A9uw4aFIGgG1faBkTP-tP}wJ7dvKA#E4GhAuP5o<zQr<MG%Eb-jgJ%>|<JFzEWmG=Vkz101h=e&qCT%I%M'
        't=F(&frdBIA_sxJxhrj9k=Y3PLwADz{Gd4!RdBq_Lx;$FU^~iJq^>m1y$dH8$Nv_3-`WNYhD3cstPuaIaQq7X;~#L+^vY3Iu!|0o'
        'g2{1`#`<@w%U)Vv_kQ(V6`ZfKXmsAq&?&wlR6;LKkadRG_Fw|g0_ExjZ&^BKW({Wzu%Sa;c;))c4z6`zbx`6ba~8p4=LeOOkiBz{'
        'g2GE3gS_tNhq52Pyty#tasoDx5Lb6LL)*fJ4UBXC#I75AKUXn@P+5#U@TE?k1(N*<86t$n<;i-#CUa7wotTL0X@Bt%g33?Vq~T8*'
        '7!kQ1t-KKy6WGz)vWWM}nU;sCFN7qm`QoWqAA51F_yWpb32!@shBcYwZ9dUCKroqGKYD`&bS{jpiXBXf3J72D91KQ!I9-<cAT0P@'
        '>;5M_^6XX{``(OmT)%DHV=@lSq2b%Gph&+oR$w<qJ{+)z%p{njvP|3#$B5;cxzi*sOF)&Zg}{)16^k?it$y~PF!lvfBMLm5o$v?)'
        'ri8V3<NR<AGTOS-ww~Ei9Wj+IsJD|q+#kY%9k&o2W(4<0jO5GM?;q@u#6G5<MyA)qs%3`f0pc-{2bzDsoduIpZ@7Gh_Jw<Tcm-FA'
        '=r+QS65$sq*S5*iH$zQ<N?-i{n`}0_wa6<vA5*{<Z3l6}neA5!3Y<cHH&Uq5ZtyzC(UM%26MLj;bu#+8_gL>VXsbP(4eI%>%F`7y'
        'jBkdH&KhM5?PUN77m67T%!P|A9h}+WRLjF6f4{lKO4|9_EA)FqG3~zhLd8%@3XpXH<n$7Sj2sEkO(U=3nJP&}iuj>*NxT4IZ$E|d'
        'yvkb~^nV0ZHbT--a;ep<mK}j>`8AKOlN5veP~MUiI^g3>Y|so}ebacma&!#92w2*UVva+Pse8q{r#}8ZByXT^tB;0qVyX+6tu#ri'
        '$8Z?YJR#V@v(leluaR(e0kZWRej*gr%K$Cn2VBj+<?8$_j^#-|B9@4NQllIO&JQ2o>2h&C_J{u^jH?7I6t28&{Se#l(g)ksHx~Af'
        '9e_*!MBsiur-nGj5PWzgiv61aKQwBz>Q0rGx{&7?I@zJ&r-c`#sQ=^p$!=gH$4B?u>&Y){CgcHkUyvcWc}>jD?PG31{hKdJpg>0f'
        ';t2Z%Htn-)&OaEPEu5ea>C=Hr7WTy6UrQ41gI8M!jcZH3(3S{W{t9r=&KdF~MHyPDgS&!%oz~~S@g@plgRcZalX_6H99Euiu@IKi'
        'j!Jn<)K^W;@m2(sFY@{K>U!R=y2kQdVCEbVYzzAnRI13{=)A{}Y;A?G^#E|$IBeF@NVT?J*&;|CXP*7Jvw#Y2{9W;1^7hOxLuC&m'
        '3IM4<2Fw3oi{pZ=pG~$CPmnx;j*4vd;t(FM&|j-z+q3Vr9Qp0js|T7Hrd33g6$TaZrQ9skEdZ|5$xkFZ`hsOra0J{Qb2nMtUInQ4'
        'QE+=5Qg!>om$|Q#_H6VT?Dvy!bnWBF5v9%khvwx9d&C9i_z)-WDL(ZNbwPO|l2yb(1j)R2UG;>hM3$m@ERce}`k#pq<2d0aU&0?@'
        'Pi3l^G-?aNW62xJdLheX=1phPh6}Zfui|pX25L;--MU{GJ@bN%Jk8(Il)s)CYNa7iInVFLQ!wRC@;`8~xr=ATP6VN%1#<q;Y(i1{'
        '4NL}+{mx`X2u~qGt2ZQY=9>L*vnwRuA8--cWXA|9ZbZq0#$v>}NX7mI7d)@0t_5Vk2+DbfVQ<6S9zMS6y*-%*H{_=nY9NYQTyqF1'
        '-jly7r^4JG#j7b3&d2}9ShRs3c8HRXL>999f#CCpJ>(Q}oeNal{uBU&#mfH2v+ZR4z?Gtrtn-^!7T1sp^kuI!Z>s=1$2E&7ZdwHZ'
        '(%Y}$`<liP2%NbRMYK5wO*Di<{@Y+**qY+>-tCrR@Wt{rs^P%sly@f53#3J+im0Y?`8@3JGg7(y^RX)T`LQU}pCe+=65`ayu7?KY'
        'q^WFd_JvXxy)sP*M8GYTnYxfj>n4Ngr?WWJe>!XLYI4I_Z{{v{beM&t%kC22e<M);_@9od(cy57n>b&W;u3oK3AfgX<uk;v2a*6B'
        'FmA0N*7*a#@BICEA*bnJWNBB*f}rA4;hQy!I`m80|ET3lIC7?Ceh;#q5Uu_e3+{IMG3>(EHG<Dw{bZ4u;V0#;U-nK*1Y|`oUMe4A'
        'D$0!$LzIIZ!?OuLic7NNHqCa&KXd0;MuOp_oXvBxz6-qW4@D)*k7a`5{&uROEwcp*gJpVDyL!SwO%*Y@BSH2`7-EJsKh#C$nmdKc'
        '-vtz2=6%j-;zfmu(h+Chc5eHgb#h9}uXFEI$>r)JiSQ}h806Zh2G2ik2<i(EPgO8XlVrF49wF`uWfn+i?_<Hz!cHhYm%tClwkt~D'
        'rzQTby(}pvbs`d3$2E%FC4tw!t(W^#JB_wntd5p{G=IDFlX%gbcnUEDorja!hOB;ZoMCP&hCOM3Y*V#(^)t$ChDJXDqxGD0F@aGz'
        'BP6U_hE?CjsY(vVphXxkfzaRS=~Ctw|FV+C`iD(f4Mu->Zvk)nBQzx=zQ!!08P#exaOcu{$7)L9J%2wwGxavZh@=`3h&99(4D3R#'
        '=|4BYmC%Dke)?xRS+sCZIPovZG`kup=mArd9NQ2&2w1zM@zV0k!Tx8a&ou*Jhz>f!Qw3q=_v~?id{q(Ac(it7SW%_{=~H(x-zQu;'
        '<3z#bYgaEM{~CdR$SiCCt6y*v`ylLo&*ceMokzDkk0^@cd+jcPVhXCu+dxG7fj2`BPu@M_GcF;x>&HyvEpPN0NDcvBL^YA73ukse'
        '1x8?6WkNU$L1vl(a1F<nnD+jqTW+JR3Ly2~DnOu_1qc9n2rpX?AmDU<8$AX(*(c+--3Gyt;}d&~TzYa?p{+YV5l+c{n|sAkF(ae4'
        'nalO|0qhf}rq+Qzni?7hgBTe?)R93<47vcx<pxNj7eY~Axr!rV?3HhxEK9PLPpV&tSBoK7Op$}$j}qkLS|i$&<zB!*QC0L$qA0<R'
        'C~!VRuyJ+v7~AwS2MXInp(u6RraVnh_B*oNZL?S}zxJ~S70umOU3n4hJn2w}O!=NGBkKu`RPydk$IK7Ew}vhaWY<b98OH)Ggac4a'
        '@Pu#UljgxB1+cuP|AMZM|ALz<zDMij24EDrX8Xi*h8h(^e&sNSZ>R-=NLuB~+}!n9r9ob<!6!VcHH*z!kbvD8;Wfdddn#YD5b^(6'
        'CSOXUc8?fRbe>wWAm{lewTL9_AS=gCKhwi6tNehl^i93`P9zE&IWizbaU0@k?w8|(c0JN(8KfTMP~HtQ6V3NQb&M|OdW!{wbT!CY'
        '+HGHhZrwSKeI^my|8Fi{AJ8WRNeA@;{iJv0|4Xy}Kdg{I`T'
    ),
    'assets/e621-ticket-bot.png': (
        'c-q`s<98j**S+_~wr#sH8>_Kx+s=)x#<tlcZPM6iY&5oW<23v|@4xZRTC?WUd^j^_9qqkiRFq^;5D5_h004@dtfU$M00jKshKK#{'
        'FmS7~0s!>V<s`pq_~cy%!sS^?rGIF9UI+eII2c171Pnq$OR8XJQ$$xu{#5nY{LFC&Q2h~T#o#EeVv6p#A~YF^fJtA%2?u_qX2!IN'
        '<YQO7ipe)>f6`j|^sc$O4BC7*Iu3gGt5RlbJdPkb&lB(*@CG3Le_C<t(1X;WC3lA1erowG!*R%&#tr!9b|uRDMPYVMUbHX7%*>IX'
        'pL(w4i)C3=w8L^WOSSy=wke3QyDWt|@NnP6z~eAzsaUBGU9~=P8>;Y7S9DxKCB?RQuhW%}L%2)}Y(L|2v;l~<{1S1<Mz13)9pe|;'
        'DT-t;D-$P&Vxmo)*ENiWtVFrjS`RTA0;@V3EkAljezbGkh%ELMwwPWojdkS0TN<hpDKEd1n%<~+Ezt;$VOAZ|0Be0)f-m)AxLAFD'
        'TfDIrDEhh>fIA2I9z?dy_vyPf$<TiF785tld;ekhZF&`k0{N#BY1deozA`8wkVFx@bu35~6zSj)G|Cvjk^<n=H5nl$FMJjBvew$2'
        'Jq~Pbo%JiCO$W%SU7D?N2JVM{XUXd<fCO&4roly<!jA$%znol0aha^HB7cO0yVD;_N?Y^pacI97mG>%a6JFq#U!!s6W(X!xFf<j@'
        '_Z4rH_G4ocef-wwkco#11zy5|zI^E>R$t#9($fnn*J|{do5w7HRc%2O`>WA<C0hDhRrZrFS|!94jne|>u=ir{^zNysWz;0hntp(z'
        'PXzY0Yx{Lj1HF6NnH@<iqiY;aBtLc(A90mE?Z5;iVgRFZ^d5UWBX1PRg}S%R58eTr5z901O)~;|#ZgMFaKNZfKU?XecU(r+kc-RA'
        'K*(wU({gS*A-+gKE{&Y8aIak(<W(MT#mEhAVg5xkOXxF=NujZADrj1Ls(?&sGGtqb_M6Ob+e@%Wp8xEz9<2Hv&P-AQgrFJmc;(3*'
        'H5@rPR^mHDU&eWxz8-8&Gc9(?dgIp(-%1hQ{vV-&tZ5o*ooeAFL_)XUH2vcaJPe);ZJF+fUiyy3LZEcXvc7W-D1Xvcg_)Y~w-?`X'
        'NIpGVz<Z{h$v*7XR?b!aB*d*VL^3&26z*oAQLfY;X+I;q#~qXrdW%J6Z^Ys&#?hGtrX%;GmVy0`Ktz>5Ios<SSqx<ri|R;m%9JeJ'
        '$d9=%`MU!@%S`R3F<tDv-g~|3Rjl*Vv0?H+4E@;czX12lRvgSjwJ^={${Pq>k1$muCAy&Gu9>kA76&*d9Z+2%kh%^NAH&8W5)-{O'
        'PYu&3v~w8pr5=z`-zbMv#FGh_nlc#u7k8@dx?)HlPJUN-&RwGk7=%I*4BW<F%+3wr6+ucz1+(HovUOXI4CuARy07?6?6$hJJB!cP'
        'VRZo^)KYy@oMJ}kQoxhc9Cc7v<Y@i(<j+asz*x0j345QoT?)MxyIoO7B5VX+N9p05ggkKTm#h@H#BG?d91`50XkQ1ECji5$%=FY)'
        'OtT{AmM<+w+}z&g_E+I!WZ%X`?GDeanMS75O=Vj$2W^hp5KVl@cTxEuXCx-Izo4YdZ!^)<lK=ey<M@X}mS}>gU=rrUvc8G+J&<QV'
        '7kQEKlpd+LBmQTmK(L;`)+otLZb?~5dQHr`{$211IR#yas2>|7hegHWs`e{o8AVu5Mwq}oSe5uEuK+C^2;tcR0UmBRZ?EBRoybe~'
        'aC?)knjw-MJhvz3%hBvb@Yy*Y<A@0LpxD`3p%sg6POBM5S$41zp3CE%O1Ie?lo%&DLm#dOVO(EByELpD^a}`rBT-ZewDp<J#NS*z'
        'oUs3c)@RT1*r*=78}!$81D1ilH1vCL-9J%gl`(z>FmD%tyWA!(P~s1S>Ms53lp|boh$=kpARL2IGa$AVTLl%-H^gGk@yuL1f10&R'
        'rRu+@Q(rgYLKLevIBoVkAAl^nv$`2ve>2@dO0;>T`>Y<G72{L@1<#}IR6&W&LFJjkb4DPJ6J#MKfSLqMJha3(sDl9u)%^(9qi2OZ'
        '*~85b+^P6O_T^_u;l}v^A1U#jqRHO}f;1%M)-=5y$$mlDoT3CQnKTN_-Y>!QIpR3$u&=_KcWKZS*cX^4WwTEyluTs+8wPUzw-QOl'
        'Kt8Lo9%XLU_P6PrCXf%y;_JERC&w}OICN)-yNIS7=Q{y!ORwQ8n($U5I36xwFV&>+g=W<>6WB5XtL7fk1cm;aJ~I$Jsx*R&*EgA`'
        '3X5CX0zw>cXvH2o7Pn}*Rx50O*{?fXRO!AZyq2m5+)pOq#_Vq&|2e(rZ9gX_3J=+d6^w^<r@vtSqLHkE7qHW%4&FC{ElYyM?SXO_'
        ')-)M?2VGP(0#HT)l+M58<;C#e<f>@xf$<2jWzgc}WYVf30nr+_j1M<tU2%r5A7b@AgMq4y0G7A3EC07O0)ERU=l1ggT-dKC^j9ak'
        '(cc*yHiB))S8V6t5_JFu_X6fYYD;BuK<N3q5MO5)!K>|%a${g~Tu4!O8FSNpf0k-LAuZ9NUV=D-Iq=|N1U(#8lHT%9r;=g7!{1h#'
        'CcmGxr&mVEh~V3q*1@Fk;KQ{A%hD&hD?Vq$ck%;7R{j#*JY`2=6^X9uG?C$uPj6KAa*CHWEa9yxbTPTUaqyHQPprv@K2WZu>%<vl'
        'R%bFCY|YZd0Zg#22Z$Gu6NG|Y8L+=s8&}5#LxL*C#qnt=1^Vbh*R%!SlzMldeYT8$pXVuZ_2LG}KQC3Md2Nc8b!fAkq)iw@8+kG}'
        'ok5#(KX6^|dy2kG17ZkFWCC$6#wI8+k=!t)&6B>cZ+>W?OM&iFbHSWvj1E<19x<Fy?x2&BLjXfaGQ~a3d^K6E^84Q>mt`3&hs%hP'
        '^<_Y9&JI?0rq!dP*HCqTB}r5uMtu1oJGPMgqv^rTHO*W47%eQ7JwEwnLON9xy>IWuy3dzz@<aT<k!bYr=?H8qMudQ4^}cy2X<)J%'
        'eH34+8s3ZKO7}5p6&)+on=m+)TLDfv<fm1LaW}M$LbhJWJO@AixyrCT@gHHvDKTOOiZPQJdaD!lZXEH(AM6=3O$XQEP!@*d`yjcT'
        '$9D0G#0l|TXM;aUEzZHuz(6YvF^_+TW?cwUYwbLcqM-uM^N!!Miv-8{tTdmT-ubB(_+s)jzs))(GfzQ?9DO@v5-?(DIXskvc$jLL'
        '&hnHb)h=D~uf+6?UPVc;L<SQ4q&W93MNDuX1y+!_&ov8$d4C)j(U-s{6FSR@kjK#?o*>eb4R{D_A5I?-ZErLRB$%p4ajykPXKUR1'
        '{rGBwPNTLP$m8%|wiRp@+9RI)7V69JUUN;fNn2fiBQkP>&j|4Q=28k!OWZ0Og;p*nf!7TmN+*dJSVGYb4&V*cuh-o-VFCY~QnSWf'
        'E&<IJcO9W1*Vo5K3)uv2-x1&&xqs-YH9xsT^3Ed@MTQjJ8Y%q<y1b;@&9rp7!e;`o5F?hLi(v;un*rofU@<>VESL;Um)IBS#KSh8'
        '`qA=}785>UMf{~>OfriOgnydzCPVrvxYGlaL;!H~4!jwC1%e?KP_yGHpMIFP1Uc0;nYaf3ZWWYFT8}X|(WK@3Zu&3@_&n8j7iR#<'
        'E4isf<5jOliRGZ^kH>aZfGwP?Kp8f*!F0->S@%s?nqX*};E|q(l>E(1$1`}o3^r|@UZVt<zwAUz&Owb&8QgU2GOTb|jhTqY?$gE<'
        'Frx6$OLVa}Oy;*lx8W37y`?IExiI8ADQFU3S<%@TUFKiVw3vZOqG<~h#TA^V4MLd4uh?DQJ-^>B$dH120H3eDVm|NJ4{j9y^4a?M'
        'Pust5OJ;yiLj7(LO;#Sjvc-@DhGZh!j2aji_7Qqr5WQRSypIbp-&!@qiWJd^cZwePQg_|#!i)`gPrVA{RD&*qwvpT6K$vmgp#k<b'
        '!NqP9M?TjRSzTLd_m+06E#$@g1ZWwtlO+l%0Tc*U5!v&qz%+^Hr`(qKkza<@jCys??Y(lw7Pf_(M&y7lu%}Uwza8jDfp_UKxGy2>'
        'Y3ijRsS{yjZOGN<y=Yp62|*JtNWs>q5ehwsuKR`H4rC2$)M*Af@^`Xr*yil4;A$C!*@>f1u-~H{b_4XmivT$Z4wfcx$)D3Q1G5a-'
        'LiXO>rE*%v>~R;o|L_BLL<N}jK)<7U3Cp186aY=i#p*ianJf#`z+?+`RNJLMeKet8-FKSTooVpdRzGT$*$S{2l7rtY__HwmiOKg$'
        'pY`|TM2%GaH(ePR@~$E$HK=L*;JI>d-%nP>;~C#aFwj65`zm<<Qt;rJXnX10U^OOhT2@)d);5_GrN38anJ(^!+Vx}zy{xhl{FwBl'
        'd$0ODiG$Ajfnj4eyKRo5I~GM6<?|-T*GSGpt_4s{0~_JKb0r(0mh~c!ZGzoK+pztauO^z%XWsr0i`*jEpg|#Yslh)wM4qfCEryo%'
        '&nL`Yi0pH^ssJ;NFuqt0NseZ?;A3ut;L&48fT2)l@<%s^gj|9e6c8DZW6$Obg%Zi1iOny5rRaeiJR$^-dne>*f*IqaI7?-}jSS^e'
        '+m)qO!Grwr+2jY=4pBujaKYI$VUNUbP=+ZBjR$Y1c_)7xpy;vz%04@Z66G>sg4@yq49b2S$f~O?js8|z;(b$ADOm#L-UAEZCvfvU'
        '{e?Eb$QAk7TNKX6>GLiYJM?AuL1pC~w<;+u{DX9*2kNoE4g5agYxxHILncqc$PLG3<$Xkr-aqmEL-il2#!H?nwzp&Cn5PX^o|QhG'
        'Vbsut6wb&W^cRqZP;WtRI($*A@0d88<O)!>w<r*_{&n>|Uc4S%4apc}#2aR{`~AlkEnKm}896rtTwTzH4&1$Sl!8hu@k$F0+&f!#'
        '$*U%$Ur8}w|2-*nKd@ci!~(akMicXVc}~`!C{#@L&!+aD1Z$r_hMKfk!2+NviR6H>(Cm`)GMz%7oG;{z%Ow?|B_q+gSH8tV3Z^fz'
        '3V@G&U8pMqmAD{raU?*`>*Moj&PxrBn_fok4r=K|g>u_rdkU-n$F<sOYi0a{dG2A}>>}Io@Y*oIfTmxaG`R*!?Q{#VXhoPL5k&8v'
        ')XG~BY1$RsyhsdD+(*Y2%f4gnR(_vm6A3Ci(Y;>{qm+Dg3ime#(|dU81LN0c<w6GT(5QZ%`0V51^D2;oHmwVZwTe|AS(9)8E1zB^'
        ')>-6sZJ|k#FB$_gy>URiRO>3`w#*;efK3wrDhtUU{K<bz)$FE&q{C8#UfxeSYuk^Ot%8etAu=s`c6PW<h82RyHn3hexb5kR>p@zo'
        '#%Xy(Q(L|{@5Z3K_7WM_p@Hg_cVd%s)*Tu97wYb`i^QI`Yp`ytZG_4ZhV3ZcCRK?=YTzS>0nF@iXSWEO_dBD&6(1uQa~sQDxP1Yy'
        '&oKujc!f6HpEvg-)its_jov8;x_X!nOmjAtM-+VQnun_F%i$Lx9a|vYOa0RoKV?#go*I+u-kDxNrCYIY$<;gj_8TuPXp-+O7EZt)'
        'p_k{MA>3(pg3GumkM2L1)4Xvz-}Cdyi^gwSm!=%KJjFDSu=9}1da?OQi+8c@^GT@J^%8nT>Ycz|!umKFG@db@@7wWiaa3Z8ZI=LX'
        '<J`{}UxgGt3*SNq1yllD)s2oVl~?V*F~m<q<YYh`yu;J<X<a5e*i1}b)RG<mRaEG9MyXSYFrpYkcaQRn&6kQJi*norhF%fKAM+3@'
        'hOgFW|CJoxz}0Qk3uh<+x4x0%kXKTu`Bss%LpZFdm&<&C_-F$4mtYw|Shz8IA^d(n><~duU{<P_>s{#_XGjJVspPQ;bZ21{?|C$7'
        'aG&JhGqrR8C0r7Sd_na1T*WvELZr^a-QCZZb>BB~0G;nY0Q2h-B=YWPG6NLD3R0m|&r`;T&}5o=BTUg%__KH129O~`oy<pIE`U=B'
        'O+J?l!jRX`hqyh&U{p21SK6XtegzIY68ZmwmIcnLg?x`fh5f!BCIgg>C~BFd!){2<QG-(o#iAqnW|h2iWdrkTetq-uMs~q3KRhEm'
        'Gi_TXleg`i<azrQ9LYSZf~IH>K+T0C?g8)x^u+K;uBEauvcp14{pSnfUL;~o!BLM`vr}5dNiNs8K6e=jy%J4`e0<mdbK3e$#XY`D'
        'O@?+kG^dQN3%EHzg=PHTl^EPl0?k_#oa|O#6!9WYvL<u1rl8a{G!_ck;^diK6P1EN7_MCn=f~ak`$}jEw``6pN(cK0UVGQ1QWs3~'
        '0fzn&XbtSlr&nR5Mv*{B!rvgv`qEWA76K3al;$}D@_crETFb|J97dxu69MS4@18CdlXLvQ5u%ymw=ONjO@63Ue7Go3GU7lb_Vjnl'
        'hT}fD;g-x{aCB}%#xGSmMYlVzMH5<!GoDG#wa}C%^-BN^8VT~c>n$6wq)xzaUf=HHoL$%P0c()}PW42NLmIaHirSFlym8N8;@!Wj'
        'm*`C>OG#dEwZAoDNc~}F9NB`Sl8KH1L;2`0iuVAu_V-j{V6us7vm_asXp*{xJ$%}@E}mc_eW5-qZFM-NBW*T_e3a@iK<R2%6gzov'
        '2})r};*g<thyzim>?6js6}hiW>QZhzI<1wJ=Yk+H4G$}-*$$Ah8~xvfM+SHDF<)w!4G&nOW?OU^mDrT;rpRdy3&+=Ao1ag5f%`hl'
        'NII0aaJ`r+w0{Pwh1&Gz|LZ?0w8Y%ml#J+353~Us@&zG5F-%zYAY?_vH-{0-0H?X}$Z#s@sl0s?dKOr>ChChm<G;LSIhfpa&@X&)'
        'GUg3;JpuXUFs!LZ6r@9lMcDBPJx9HGy0U3BJfNh;hGuQh3>_h)yLu>PFKlA=8AQv#2NF89fP@_|8x(9;G+J&nRQ#npW!Re2jqN^Y'
        'XubP!Hl7fjozm&6L8Z2ck;k&|nzLh#+Zp#_Ws?-j>#~YSUZ7oFEE9c)n5y@l9`=hE+hT6T!;~Wl@FbL~f?XMpdK|+L<IF=GNiq)0'
        'Hy?#xC#9Vr(T1u55>=cYiued(PW6>dR(DUE4b#`|$%dxBO+DefjZT(iOONhNsv$46?qfKRC{|=FC>|i8qL6<{9)?nBt`fB-O%GfN'
        'p@|WSpd=Ai+Ru?<zNjFa)qop$imm}ZoJq(%hp1~J6?Im_LGauCiFNSl6T{V%hD7QqmkUp%m2q8A=hCCLx60#Uzi4WFSvtYID2tL*'
        'ha#5$PB7q^y(T&o7*qW$WkrfV^@b%J7Z05Dl)KnP2oUBb`doY17^5BaVNY!u#qnF*l=aFsE2q}%qWNaWrRg%A13OF#>)r<XCRvVy'
        'zw2#NDiowfh^q0GBA@|k4~Fa(2KHy>dBZ64x!s?}p*krOjidjPPK`5lkvb(QU=NBMq^btc0OHTQRXoQ<^HB2{U=NHwTrW3TFX@7q'
        '^(*4+Db{e*BAwmTs_V!@Vbn>-sva1ysc)dzPhCjmD<%l-*R%)lLw}(f%2j=i+Y(^R*dCQh&B+9<kW%Lo?$}1=|H%-0lj|c6sCL5W'
        'Xh&iV3lj&T+iWWTas1}S_%Xg6wE_!Ao>fXk0#))QFx%?6k5@`Af--q3v{47tb1!SM#?cp=-B;WaLhwx*p-j!h@Cm4FfL#6vrNqgo'
        '$?6%Jv+vJ#3=zkxK|&`o3_r(APNENg^_Ef;W$J4gdet_U2Z!fY^&_j&GXSf7LBDB%42FL>%g!;!JVju5|EW*Sdh$c-=|y_BV;K7?'
        'em!Uu@Gn26?V&2;8!=V&Mi3ZXwWSmfFUC$(K`lLkH=lwZ;vX=;i0JNC09U^xWd*&EG;VWDnji~wsBCOI>6pTHdNHKr@xYKC&SZY~'
        'P0K6fiP4NcU%0z@tjIk1c}(3L5ebb5Eq}Ck&_oh^=0nI(k89@3h-DhRvYRt64@&Ut+c<?M1Q5IP<sB;Y9nHYz%2CrEqEC@`UWfa='
        'VPZ*JmRnE;qE=_Z0Tcj#;0E;7;e&E{EPtX(*v+}_FvJd=bgbW|pbHrD#xy+;V7G}{+JPwLf8Aryh4`M@*bcywnp=+g6+UgqJ}-1w'
        'gC#?`%G4EC0+UN(<>Vg-;}^iawJH}cHZL^_Jeuj4b!n)10YH!3clBxg0&rEB8l{WQy7`;zIK&PnC~^Pyx6pa{2sP9P`D<47dLRtC'
        'EBt}?CDG&N*km@0^gr10x5)MH+!K*JN4p2rbY|V!jp_aUoUfVU?ucA)D_+7$Pq_LNF^5(2J1d^Zs4lApJI^$Iw^Duc5+PqAR^?|e'
        'U0CoQ%YF)`6ue=cRW|~Z(ta5r{D4Xxc+&0rO!_jAI}bh&t0YPhxOm>09*R)|SnOd*wQseH(Ml1>h@o$wjeigsyg~KYg_%{%MDG7='
        'z7B-^L!g4;uH2^V<eo9)6<t0y-$Q<l@mpF$0zA5otPSVcJE@E_#MEg8K*j%wM)K?4HorBO)e@Usv*mk#K^8M7MS7$6hhL^6>>sD#'
        'RvK^~QN=F3ChZj-E%})hlwh^}<7w4yPsSjnAfY7;Dytquf^*_Y_aKHEBIO=3^C@bwj18V|?etc~G-aSUqer?uQ!x1u1v(}H1f4+}'
        'FW(JfMWX4va)gk;{&pC9Mrd|zl`a41D+^zMiYIDdr;LJsRJ)Ch?iT0{dz9^2fybq78ZQJ0BCNf6;l@fj`0OQcT=`%g2b0C2vTTtP'
        'Tg+P!Sh*(s_)WLtD%fQv-&80wmU%1&jsqX>-82g2^!2{8CpI2BpCP2O+b@8vQ{_|mF>^eAJ3c4xWQ0-j2Wif<w5r6;iM_BAl_BHg'
        '*U720NCzeVduyu<0Z?w<m!9052&Oyz7dnJ<-9=!*U*OvV#?GZR;Ya8y9(&yfvha<l*Q!i0st^EMA6S6@?B|gN*jb=i)j;^U5V0r!'
        '3Y8a`ouxwE@d|=pv?h7}xD4q1^m<%SzK9Lq=#R|0vUhAFW5o>M-t`5npE=qx_*44fc)i`A>OXag{%2N?QTufMr2Bj;@$7VeOyp50'
        'SvIJgFx&}q8go`EP`DGmUdd0<I#^9$U6&QcB9Zy4a#UFJn!uN7e&{GASB|#CV%Gj$elb>+)lHh4fZU-+N5N$KZ8TMKn%%H+Lz2Hd'
        'sn;jiAxhBSK`YeOJB}YB#7yqwPae)p<dodMkF2QPJ_DA=m`-JFXX6Wv0F%D~K38^Nny6M$DG4RC&Xdj{j=i{{A+Wgr`r0V<87J!7'
        '4$vX!h4|NFr|%isSQgK}&cEm1IwZ+EHhoSg<TgWBJAR@?GR=HID^mwk8rl&TO~U7i4OQGkOM{iZ#OWj}SPcrMF5Kka8{|V4?oLno'
        's{3QmRU%Zl9!R?#U=xiy0F`WoA(>Nm$@LROTl=#92YfuZpcELKaPg?d*>on8!3N5p&yygbu3dhi=S8Qg1~2kMyFXv}V@nt=Q$r;M'
        '?;z=m4jZ#St3Sh?h_x#=o{xKZar^%oarykvqM0C<e=%(_Li>5iE4B(`r@X9^?6JEqUmq?)&B7O;vSv~B|7PJ6OB}oiPyNkRbS`9i'
        'Pp~7xxDAEuc(7D&wTUPC7OnwFEC1DqcQ-$pTnz*VoJ3cnKWo}HrDumTP_;=*2wJ9d_Me8ES=?_`Pa5#oCJ9Yfs4&wqlssZcCmj>G'
        'qy)tNh(WHnld_@(6u7xQ-$PFecK}~+<6Mr_UtKrz^>2<}c_=R&;FSHXxJAVyh@)Vfl@y1`-ro6mSJA4ew9AcS{m_%$A3OAs`A_wg'
        'hz6!h)3$;<4v@!~VVh+Da96w&;Mj`^T0-&y&tkn8_g-)5$D!d@hG}J`-uO8254wv{JW*Yi-NqWw>_7JdJof5?UeGb!nMGtw5WIP~'
        '4j?F%AKIJRDqe2Vc;@9RJPn_b?;c{n*@h*bzm%|$3?(yeHHS+K7r)zkx{KtQL0#1h4MzEQ8o685Yk;#*1@M@(8x-s}W%|4xEF9@i'
        'wp!H*a9<%w;YOb+roivXD@a(7glk^^{Mvs$&n9hAiY@&6(JFQKaUHA3&3B)^%m#zowz?MTObv{e?)lW2o<cCgr?ZJ!_5Ot7#>Frm'
        'P0^LfM@h>^7BPHWX4_MZk`>lOkA7=>#{C&#ztpdb#O_|v!+sj~>O&d6R~KpbD2=M8#!2&ILT>kbZE$4hcSS_6iez&TA**c=zsOU^'
        'ZyQ-z>|2ol7Kyst&K6AhQdQMFvCqPQ$RO6mi5XtzZ+mGodC|^q*Wv+pk*_=67eHkinKJ0T$x*mXKZDGyNM&rVZ2yI)IMpOrmNe0K'
        ';n<aTOw+$2rR^XxrwWXi2!$z5Q#)aoMe&`X@;;8Z*Q+rDbu+(1dNB_(Y!LQSXL3@9Sc!7yT}-HJD5DQ)6uSOwk2O`qU8LLv52J?m'
        '$8y6G-jOAyemG`+p3LLrSAZHobc}gO-Dt4N{6p{AUF2vgKa5D^i7?=@%U4#8%<$lOA=Ei9gi1s^U(ZSC+R!(o3=xv<exhS-^6}=I'
        '?7JNx(%gNncI-@Ny$eHE9SCJtggSbE?5<yIv<myv5pQvNQ>D`jO7IDo>kUK`MR_~p*;x>U4f3jWbr`Y=>N2lEse*n%Ew^jsg8s}0'
        'y?rk!(F4#u65@Rq%w1#CLlmep8g9wWLf|tz<{DAp>!M|g-&Z#4UWq2N#FI4bO)<{ILiqeng}0Z%mKSoTWzh<yOi%S}j<Qa7nVabP'
        '5sMu36tVEbVw^I%A%y|BV6J1vq`aFuuKUjD=1+lZK}q^;R+@Cu$G!A^JGwMk>2ReDO4&J~Lm(adF1!`D#-+i+fjVTzTW*Zbrf|vL'
        'Q-oSFMn3N}(nM3n69;gJi2mrIs4j0Q^UY2diaix!Q2PvZP=t=Zrd_nstgI_(5LFP`Uep)SNMJ_FTD$7+0E(1$29NRbUv#Fd1gXBc'
        'Y$VqUVmRKQGmBE_u^Ybd#gm}pVs{GhH)ej4r{A7>U;mh5<Yzbtv{BfZN*{}#9t2|i$~0b-sR(}dR;~mREs#~QB=WJVKhPi5q_6+e'
        'Ub@0r(gYCkoXB6z4i-1;`>EakDbDjC7ZK%r`b^6U*{-lYx9d%r{zx0Ci<O$p;>P;?7o+ZSPmGt(57hQ%Za}6Ylh&bsER+MRVN38#'
        'C6~)-j_Ra+Oq)#jWpa5O!DLLixD3)MkAz0JQy?vJXC}~7eh&*{tF$`5JyzA-^a!ZI6fz@ivhlixEyRwV5?%?$Pl2c_FIba6e-;4g'
        '*R`2Y>2SIU9j8>N?f94nVivwhtKmrOFLl$@*9#;?Kv~=6(C{Z8SN!_A=3yjx_i*?!h(0hEW|Z({QHJBg0;}g^Oa11Ll$<aT8ueR8'
        'g*2mu5P7Oxp8{=D<;hL~3u2oP17w>>eB-ySsXt!Sx$leMz2VE0*glgMXmrxkb?H?(q*QQk-p;XIzVATtZO(om*!SDI4Ch`H3GZpZ'
        'oqUmqohE6aQWF_DjMxKkGZg0#=F%L}I4<^6t>ha@{j2#&U%VMa0?3;kVq$&v#Bg_Q5(heo@6)V32-%yg-s%6Vi5@yWo(m-w#$OKo'
        '!6HodL|c)>xUbA+i-bwmexlR$Is|**=F11w{v4qutM<&7-50rx-u7V*=kzj4da-p&?B&hi5ve9P%!QChz+iIJCE;>)<L+|h%GDH*'
        '5jFdyik%j&^${R<<8cTwaPc5v?LJ9={-gW>eU;3Wgd)f+`*d1ct;@%CA+|4jGAIkFHh7+@1XYsaSsKlM5s&XeZTbu<YOkfk$wHMq'
        '1^B{#j_QtCS8adgem@(8_5?iANQe}%Us!A0<$Mmo<`>E@gbL7OgO^v*P&ei=qcx8M;{>-HbeUtvYXGvda?gd|cy|804BmGHYmj~h'
        'U_`O##|8`gbBOnvnEl1+QRedD8J^2=zo{4P0vCQ>!(OcYDvhFLqD?TVcsGs$@HlS%7sLFaw{ryHt_9o~MBdjUva%88G?Q^i0yToJ'
        '@M7EVf3`r5qcZodgM*=k1iK(6#JB;A)q_cx3pf7;l=6wc9Q2_v&{M+ddNM-_R-Tkr7b5gA?XC^MV?d)+IiI3z5%OJS2_eZNRTL!A'
        'y@uY{bC=1*$a1r->4}Y6k>!#t?}W+*DZVX?5LZLc32DpJ6e|DcsstL$S*yEfpYT(D^9fP|7r=gsmWXo9wt~7x)V=!XrBvuSW$J_0'
        '0+u(7&7cX{od_pZqe;zFn>t{vHLlkY!F3tOdp}q773uL}1Gd1`la@@`*QB6QL94o$rW-RZ<%4<O%=la@;QZKo=cjQm(UTKu@|1?s'
        '%N$IAg%lr%WojbX$sv@mU#-ueyl-ioz-L6xwj!KjoQfpsd#KS*(DR~x$@g9QRQBluh70J&O(;Iw{Riq@Jd4LlijR!Cz}=)>g(7nP'
        '_0I6YuBEd*vhIZcP+9PSG<PFK1C`bSE*STgexi}60#gOd^D=#UYz#Zn%RHbWP+T$;C3pHs^>rp1J-9YEqHszGQ4w}shX`Tp4<3IA'
        'ma<MfZ?6YqY=pQ}V`ss?1=J6SB*y3MreKrxUmiYBxnvCP*}6K>7$(PVY9H}kvLuHge&g#tPcl&)JZ9yyw@|kXKzAk^eBSReRO@a#'
        '0;i4#XBYZnX%tb#j#RpvgahU`>dygkJL3gIRT7~l3m_!I8{l~`#bm2Zst+O|ivO2{tt0*wCIzycU-qzefF1-g)c>l2!i|v^vO*JF'
        'E%L8)P-h~lM@_y@-D)6%iULTW4pSueb@^A?&yXdioP{bR<<!2)zI;te_%bYX;jFBH?_?pIyin7whh65gr{#ST!9&QsYMJA-kgJ6x'
        'F$uLICT)>UHlEkd5AkSkBTu-;i&fZ45tQN>%}CaSv}5Ig^F_-h=VPz8-s?A*wV8rQE;Qw$TJk@PK;e}d^lMR1;D`U_K00NkFV-Ub'
        'd|9yEy=lKl9X3g_3u9KHYE|MD!(7XpG>Xy0=H4695%j`b&Sy@1!^6se=vutO*A*&)%n*c?FBZLZtpDb8P2L1|t_pQ0`~^xz=a8r@'
        'L4;T^CdkdWr-fcywZbDa$tu;+x?vftaWJ9gBqL}mEKO!Dy<{w_2O%cND3pE96)Yq5>S!=@q(c~pqMMY5uURcF7)EV%Ku&al?l!g)'
        'EgFN!<*qn1qbKi+tqvTeKw8WX45O%I7fuF`R;N+8&&$U4%VzZ-MIoj5H9-?-x2n%QeYLnN`v>N@dsqHsFcTF~n&Ko;Fi0+t^8YI5'
        '^`)iDG&cXIlM)6D8+)dvBeuR_XNJa;P-Go(aZi_LQF#fW6N0Gi17f3D)UOQLSA5vNkSw?w#X*hYS=)6YA7L^n_XVLEJsR`7lWTG0'
        'ZzT5rAR}}1AsDZKy)-G;^?%#<2G^{7S9rhA10(%oW?WZZnZ3JsQz3EWY0W!Gqj;!y)hNpc0RkO48uG0j3<2g>9OyaORtLy})H?Kx'
        'dHxrQlaBI5=*e5m?gp{fbV3HM=T<^*kOh~sz3gB}5B!bvXvq}A8ogq2m4fMldhX3XqQWI9vBNxf5BVOz87882k40i)qpGZPd$dCN'
        '=N`|FpL2~7F0#+HN8By7%cv+gA-7Z|=Is67V!4=;z|ahu)HB$A`7PDm=Rp@$wn$xI^)JmshBgw#raL-BSzI8~jamfiDhO(PNMd(b'
        ')TdX8lv5$x)Lc?@-m;s8h10<IO8@t9p!{<W{JR-_tX1m09zCKiq+A_44=<vWNrjeUY*KjwT{Pbpo3y&H-1;+x_o*ndf+D$o^oPuJ'
        '*kF}c(Pt^SyKugRkB@$I@aICQODgR<H?B?hj{+7}l4S(BVl*6VPd5xH0*eOtIbHBc(NxsQcZmDJl!xoPBhgJKT|ilCS5(7~dk0Ub'
        'Wpp}Xex&RQW=VSKRUeuXG=-@lrz?~^-eTXkb_C11?owV`nJ&qbD!Any%+)VAvZ<E*w%Qqow@~rsO-u)?#>uOjtWC8nLk*AU^_LWn'
        'p3wBrwshHVt~YlpT^?ofCPS?|_XB+Xys96~Dr#;?wXeXmUzu0StEsR3vlr<ZAfg3zmxl|SoW#H{nPqe>Vy5dFcyuf^VTj{zY<I$#'
        '=mkUEZK5IXGuMDKp}r?Lif&n8HFNSLdG3LYBIjZyA0Za<gIr|m#B8$MK(+m%rgs8?@-dR4g6c1me`$XCAw-OM9?^0C7C3!sy1%8~'
        ')L`fVl_AItN{XYIh<_>I2t&UFRpIVzJ@jq!f*N(KVneFrAu)Jh=QrHd41k)d`>Gs)1}St$LGDl-tmJ_m)v&Co3NP}k#x;$_4GG|b'
        '?Z{@>hY`?ywes&z?Qa#dnJ`K#?ge<s+{jIYlT`ha2^_~j?`gyfS#EIDu<Fmpd4xJOQ$KSOTxE(j_^B$(<qy=sE!Ge^?*o_n`RxD3'
        '`;n=RV&=3l1O$<`x(&vaWqh0nk-7wt7V}UFRy#CmW3=r`a19FVi@wOG&@0_MilL?#Z3(gxP$A;8+i)spO^Uw63GTR{87DGVsr+gz'
        '^k-E!*eychK29kF4Jfb@{WX;R6(CV`{u%Zv2)q4-_CH`|fxQfg4Ylgz=ijH=!VK|&+~RFx(mU9mRez{N3fb8f%D#LQ_nTS*1?j`*'
        '^NA!Gp&*1LfvHFBEDv}+ZcCC7eG#uOLdP`1_I}+`NxdsGCHTK%c?Wf?)ThVUu>ikK|45-sVV2yUo`GSkbwubA;G|toBa8LT*xYZH'
        '4iw*-n>=@_DSv@TeR10_KPMA@pS97RMs>h`yGlRQ0f*dRSeNL+umipzrK^p(;%QF`<MQ(s4+fQL0WglfQ2*RXdDC7mr@gn7G_Whn'
        'FtF};tUMr|$lS>3c;dVHmCF*H@lI7Lx~LEmkx)AGEiOBCg}P$LSW?ialzP375lwoEWCtMHv!($xuNU$1<@;cX@yZq+#o|~#*-tex'
        '&CZry<eR_MQM<nwkVdQ?*?`&Ct$9R>1@{-5j{gW4t}IdRZy>o-@O};qLnObYk@p|IM)F-q$M7-B-kY+F>)MznvepqoL}rIidO|RU'
        'pXp^4S^P|J#9(G2^0G0GMzGq`!n8TIqrZNPsQjBXQ|K)m76p<Q4(NypLYMamJCS^JJCSk<F!S}{WPA@*npbgS8DIp5AO=}P+gq^='
        'k93@tBEP1?9j>p~ZRkC0mV5v;T6wJ4w^RbWkKBZE?Znhj#VgcGpX@jX8Nu@ZJ{X6n)@+||>MT1}t%WMKeuZ9Wu8WXHNAM<2$2yIW'
        'FLm2CqfcR(@Q_R|1@u&mCehx|kOrPjWD^;}w-(}%i#t?6&gyVn#u2k@u#Tl*&!L&2^2h4%dxVc^R`YA%(4_GmnUG)|_|d%t0dOVK'
        'XIF;v?~UJQV6$*+gE>+&9^p>ZcE<>c@~vG1<<Q2h6y@w?BNbPuUplE&I+%k8uVe`l=N#&4w=AOzr}z-@uKtE=w_c0gknhCiF*hL='
        'DfR?+!@JloQ0Q>IyEM5tkOT{xoNRO=sS&Sb^7np9#6quzBO;5MTr6E*Z=PRr6g`?;_K4GY!p%2|P2<78vyPdO8b+Y|BO<=ipMPm='
        'Vx#*(NB4u5E@kGJyaQG2U|kg%`j%o=UQo8ZBH0$XDDzRRXi#CF$UU(iCBI&?sJ|)N6i3V$))rBYzrgDahs0T9DJZ?jGK%YByAw|T'
        'FAOd(aPg*g!_;HM9^-`E4qlHcPVD&TdZHiRZmTRV6=GL>c@{oN0%rE0B$I8$33)6L*H<krmHpJ}7{VJSRylzd?~Mn=TuaBenFcDx'
        'a)e*eG6!r3SYkLi>4Dp@pK_k+TAXe0P3<A7j9^?CudA=~D|CBDCdi(YeZi9kk)1h5VKG1OKAh&<Z2lYCZ;<XqpOR|KZWD;vds05V'
        'ZKi^*e_xsM{p&`LP}+o5)Yepx$Bq3C*qiRdrlKp1m_AR?4gIg;7_~E@9zuYNKWTs6h>E2nInGXuGGUCE-EDW9{^2vV?9Wcai2EPA'
        'k3#GZhW&x*rwUtAXD(h-y+t2THP~&L4k8pIh9_opG8H2b;D4nMPvy|1s4B(MP8|J+S{EESZ#TiE^*#QjXUH~+uBnPODNw(ys9r@<'
        'CS~4BB3y$<eq+c0?2oDR{Z9Zvo&cJXIi`{Xp^_n@P%5-CT|cJWH$-t_=vZ~m-s(>(_a6ULoL?t*H;DZNOyqr^OEV)gR?GxAgSTH{'
        'aK9}q9&kvt&Y&hI$}0J$A!9rb#i7<2i(U}%a8CU&4dYMl3$}ZdkK)+nD~4>Sly#|8U==I=2FYpLJ>BqqX~oyaf8{Br4&w9YFBn%`'
        '*gCz*dN;TE6hL$N8!2qVg#U8Y(<~)ZIn-8d8^hN9P7Psj-=Qx~hg0=DPhfnA{*QJCOSVZG^fkBc<dApClUCHnB0X>)h!xmw_h)x?'
        'GU_Xt7klVJ{15{kag{sG8A%8%on&BZxh^<-pMqJ<=GgB&7llYe6C0o!sLGM-zcSUEG4R=tD$D02a4Ik!zU4!?d#f>De3rt#D@(u>'
        'N(fVLPQU%xf8k+JI~cUf$N%A11jBK<+n=BhNvh9x5w3J#CANGxyagRIEAKbQB}UQT(lg8D2gn@7uMmWLv?hPOVb;K349ARC4{zgN'
        ')U=6YaOAO$$3wwK5P>?Ag^+~!X`9+bnoEa;ouV|Xof+1EB<CNbM4n6^AD%Yr>u;lj8W}|ZW}tbDy55a|7(LJFV7V1xq+**dw?l;e'
        'Ys~bX@#nr89MQ>91Fr3Zdj?m8pctcRSJ&aL{n;Z+X~Roi4VWE1D+0(}@72(sK|C3amG@o+uE1iIq28V#5wEuV@Zio}BS;y*J$g|c'
        'wJ=%|ek^b3b=!?H(Xbgn_C7}f|JN(DQf+W+TW4@KO73kSH}t3og=vtrMW`<hkT)5iMB5`iuW3W>Ul`jlfC7Ig&0xs{$0pm*?$%|f'
        'wDY<3el<!;ghDl2RRUNi6Uzg8|Jr~8|1wgc>otuvIYWm1@a4_s%ZQfXb0?Ojx#TW^wsDzT|NLQr^)b)RoAX}l;V@rRFm!$8i-}g~'
        'OP`o+d$RkAd!DuT)hzbbbxA+7i1lzUcSRKb;O}l3)`&~u-s7Rk8LZ+*cTs>}!AR8h2dBYW05vB<MC1X%GX%|fG-UbGW!^R#A%I6f'
        '3>2eO(D&){phMubCj~@p=~3n^KX2n=h-B{~K-aDhHl68a`ma;EB;hO018-)CO(GKi{9M-grT8Nw<9!iJr@Q3elPrv}CuU3>3=(wI'
        'ev_HwEU#)+bzG-T#&*(CU@jzeO(ra=gi#(0ok54W+GbAIb0<2pC%PSooppsY`?=${m+?gq*Yz+Lb#Ojn;U?W!H2T|2mq|3G5NcGc'
        'MKnF77PLUAoLD#ob~!{o@?RY8j=M8I2AQ8G6lhz<I&<J}qJqitvC^nuV+WRg!8RgY?qW|^4A`pGgm}Rc1Mi<>>U6dc<t$AF<!+-g'
        'M_C?75@PgtiT26)fjAdd%I|isy4macoREX#V{qMoD*hiIyvg%?%n;?rSr^+~xPLZ0Xczj=9Pn~p{Zu4(e#iYc7prffd-!ikM1Mj5'
        'hbDy?8|X@>mTs%zkI^E|zYT`3$%s%wsdjy~6Epp&Y_-`gwYw2Kj{V{gNvLHbKidD?cjO+rZig=+?)$tg9viXB^XX>&^W~=U-ngSb'
        '#kw1K?1|5i;d(`3(00;nJdZZ-sfOz2N_%Ex&ldGopn6*e7F)hfLGZcP5p?~$W&Fc*pO$Y;QrEBPj2-}28z<=MYnrJAi$S9FX<a3U'
        'DD)rHThx9jw~1tWqKXJhBk>>oGeu95Bud_mLt!*aL>kldoQvKmSpG?meM+9BEQt?cl1hGWUTE5FzkPDMd~|kNM7x+(u6rv08`0Tp'
        '`*Wz??!gX^D1)i4TP0CDEV-s?MNJC5LEu`NZr4zZxnD^Yv-&-p3H;X3El{yV9%%R!d|KQ{oC#eN(g}srbWlZgb3CDCYSa-Opi)37'
        '1Uofrn-t>Dih6*36(BOimFsPGyv1`do;}COgfyA696pFUIZz#E@THvJ0-c<!DhPWg(r=@m>lJ$|=;6Knv!AZ7dD=#PDhkC-QU1tG'
        'ZZ)RUJ(oi`7T6S~3v{s(de_x9vlEsnh6H&n$uw%#YSZZ`{3kS;w*=W4_jhQjnUaJxictEr$Nug@pn%D=9~@Q)W1BJT>U@x^%l?Ir'
        'wlV{aHrfMfBBqxdU0@LU?TWSaaNXi^7nMP=dw?Np#1SeFQd|pL6r<W_&+APCPT<;PRyY9=VYIC;Fm$bos=M9!Q=N3G5#S$SbvA}v'
        '?<$oB{DZWTL^uAi^Cdjh)WgPWpK|#Z48>zd?G$*;k}g83=3|lmgZjcFIw^O(YboBsYxuRtwTtS({?2O+X$FIaNpjOKsk`6MJtp&A'
        'C*hHvFCK$Wh>;Ad)XMi$F<s_~+cv$(Sbu-me!G%W6oc~Io#WTe+xx5twzAGlGGL7=(h<{PKI=Udeu?if*9nI+N<0V*S$kkj%o3i&'
        '6*J$7OHl$<{&MWse-cJIY}aETdy^BHqz5uSt6ZG9vZ7u_)yQa<Aa!Zxe#?nQ{Rv#!M;G$`w94zg=BK9vY7GZ?oN@Q1BCpi_Cc@i>'
        'Ap1~8P;1j}dIN?SUKW=!_2N?YN)kRQp1#Cfn2ct8fyLp>{?g;J(9(YP(i=*ncyHQeT4$Qr3b|P$`4p4pVnflQ<c{gz)1I2CNOmEC'
        'j!0}k{Uq&PcCI%1Q1`_7kMFpiy$e!~Wk)8kp0+_G7$ID*mnR5O+glJM{Ti4YwWmoO-NEn$PMQ4;64?n!EEI|0$(AO`9yB{~kaXRg'
        'PG&f<eJ=!n3I=3?-oNxbl6i-?RyTGN789y9&)+~Yx|R3ie_f8LV=TuNqesg`2o=aQ<~F~6F58LNsKQl(Y+_iMb2pJj1Uw&g6RLel'
        'X<)jW&GELDUZAnLo2vHy6Lu6G5O3_ae`NITUDT2eh<eFev{(r{EI*mM<c;0Pglc8F-rNJ>jz3JGf_&aLO_U@DbR*7}31T+-C(Ynk'
        '*Z!@>#i55w@q*p`j_L&le|1h*2xMhJ*BWGL$fr!;awEWm$M2U4u9%w!1wX8muJiu+sl3zUPc~$0{AL_*mk5QfB;IzPhy%nqMwXFA'
        ';Y$9;avAnmzCqpat`9f;Hh=j%arrbLr|QSvn5FTBlpF@KYk2PJ;Z*1~)I)C-oCc{U_^Nm=%G%pigJ*0AGrQrCi1&<SINc(iw4>Jo'
        'j8|i2hl@jUd>%?1r8KLv8{`92e|P@e(h4|wZQE;Qx*E;-JeLfi`3bSYL={qs5Wy@aH~y;lwA6C?-BeG6o^e#=h4)^6ee_$B$KJ!X'
        'FW~y~hz#;}#ImESjEAim=b<k6(H?b}U+;5$l|b*(R<pbuE|F;#N&(Lx{HpTMW6v2sM^aTlqG2eG{s))ZhcCu&{>Lny$^mAT@JUnA'
        '{vL^c!N|h`-u^X;du<Mp3L%LJDT{>ohWBTw&qQf1<n|EB<xwtb{|)CMi;E_~HccE+ozOs_)`JUnE}*gf=<aV1Q`Bcr7gBJ?C&9+k'
        'Gm=;j6!Ybo`RtDxL=sr*XNPmY$s6|*msA0i=c1@HTU4mCn&Vf*@p}V{Mb7d?iFSy1;?K?c+z}in&F)VwYesn@T>se5Jr!%bGheOJ'
        '8T}}jehzNty?Mt^F?X+w3Z&AfT49+}P9w(e<J83#z~EpTs*ewd`m>${Ric0!&?qoE&bOtqxW|^!%7RbLsC6C@oPP;)gL^btnD*Le'
        'q3Lta{YAdLW%UP~imJm?eW$lKLbasy!Y^ne;og9gFR<UJ;jf9nY$cZsz(-@=VFE_^EW0^d#tT<zHHthlgC8bxx^e7RUUsXGdnRr$'
        'nX1^wQeuU^Wyzi(sG)&!zWQ%_>(Qy}Ks@1o?SyvEm`zR6bf~#>DMT>!PtKz9O+8D_<qLwN$X3ImuFKQ5k(1@mhw}LnbgcDWG3h;k'
        '14-YN3|!=eEfXm|^1yk)rIP&3j6-;Foyq@U>#Jja{+O;v-imP4+#9JXJ85fpH00l8l@NanL=JqFsr;Tj^tw1K`j5lj_nOOYiZII1'
        'XSZD+xZl&h)QUCQJEQ-@w7yZR;Fa@;ZyT2~Exya(=PYNQ(3R6+09TGLNx6rPOU-sUi56CF(SM}dWaLhPuvy#5J~;%JHB?=js6zV%'
        'qF6^49k@K^u!St1I<Z8IfO<t*o9=qy&&mGIw?ICM3&{rmM>32+E+0ygAH-<EWJ1~btFqV(0Ar2=@nM?UOecu_CDMPN9t$7;3VoSq'
        '=%yUA)+}I#V&`btfqqhK5A~VWqMY2|&lP2dS)3+BEmA}1^2`p)n=hHws1hM#`t@q>xk1=xrXivIHaRmyHSUbi#gH(P>|MLH$m=yf'
        'L+NOtB>;u~@72c=@tq~GkoAhR`yUI94kHrXLUfaRp;oK}p|%A-A8X#rDMs)fw8$)_rKw!vFV_Cb<w7v6YuwAuO0Moi`6~l+l`|0z'
        '42%A@yXk)-;W1Obb%~^x8n{{+t%)40ET%2G^cO<wDl`d-O>~2{>Pvq{EEQzz&u=haDd8tL_h(6k{=>W#_Tw$B_fPM!T?iKC2?onT'
        'jb7dHv=4vn?GGCYysvD(Ko2CoOZ!N{^Zt!K2t9l+G<;h>|D|3v6mcN{e!ELLJM1emA!cMA$o+vH^IHaJirxrAS3#I8R^4a2_qGrE'
        '_JB&hJZM^IrX8@8l+G%_6qPZ7b3>03G>79fEE`;<3?37sA1(Y4c~6MyPelf5s|2Z6JqJwZVfDF^9#7B3@;!#9B}RuEi*C5_T7s|v'
        'aWw~ePg2Yz?@0=%eXtliIRrAhP;uI!OsZ7wc0;%7^KI<YjigLhA3wa_^63R!{f}G5k`WJ4WAzJ(BS7@kh#M_1!z42~?~)T?M$rYe'
        'h=J`73&{pPioO*2@GxS*SXlA2FvWXCtzpnhaATe-&m`e~$a3Cr8y<9$b6@>`q`sd}2|h4LYscBK0w=%fmkGRgX_`#0l&Ou5f$hFg'
        '<GZV~T7D<MMN)YeO^p>SKHy$MPg+hXh`NujroRvd5a@$wV!;YAtiw0I67gIlOnI+lXX+4`z)nBni5{gW5QVQ!AL(K^k-35Hc9C(A'
        '<^k5l6c9;mK(L=u8D9liN&4?zcPyKMxy9p~$%ani6y<OIjyVYjU8VAAk_kF)9W@Fvqr~C-G1S&o6LpdUh6qtxbQL*D#@k~8bd@4^'
        '`=a0<NzKk(z26x0Fp#$~U1YfKYSnWyWy9OtM&_}#D2<sz@HYq9%esQ)@(xi0B~g3z2oes9^5sb9TPmq8PQ<g=#OL)ywWibBlE=yQ'
        'Gl$MLTAuaY7y~vJtQ`bl(Fpt?@?#}Kx22go^^HdHsYilE1!Tbi&C$76dP$CK4loM7KX>h7tUh^RUT-3c0x!aQ58(9>6!YN<9L3w6'
        'JFn~{!z=Y3k_W$JhwxgG=8a^hRHXe}Ax&kT6|!_@Kt01zy})*951rs6z7=cn(0ET54;&P<_~G88*L}on5X4_!U0Vv6?Funv;b?m<'
        'Je>R%Cq8E#@2iJRpCI&|F6ZH!`)gJS;!hMeWDeNIYM@bAIHsE5x()4I3&NoTQ$%`%u#bKaQBa03wgF*;&{4EIDx)kLhV`H1T`IhS'
        '_(mfiY{HUN(cS-KlVrhD0RjK(=B(bLaGy55bW4LYDBUTrbV^BgBT9Go?jo=v-3?2ZfFejY(gM=G)R&G$YJ~+}e*eZhXV2MO&ou`#'
        '_dWAL<l=a(b|tB3^qz3scwb2n*HYiO8nSfjE@4d)5)CfF#d3WVPnUHjEJ_w{=F~?G3qx!@giEyIl)4GU@IrW5QKD#+IT+APdZ6nV'
        'p%`r(#TcbKTG4_Z_lJAQlX1Q$vD9f6rj)}5=f1x5K}{C_!s_v;57HFz)Jidmy{1B<WjD>z$0mw?lN0`;J1^p@gEi{^o0)u<&Gi?U'
        'FP1s*88dxEQdF`U*RQ$Cdgt~d33rsl)76gG^{P7Z{tcm#MA(;4HsL4#nT8F%sw#o+aKQ9ellAkX_^Io$n=%F&!eFg9!(aGkDTC;f'
        'DwZ=|4>Q*NF?;2@a8`z^#xhs4nN_z`VwpI?J%Sfg@5B>%c<1dhR=UJfv-(o`Q-1`vWboM@?)18Hshs2bmK~1lpMPOxfFTEVHWs69'
        ';y*O<Z3*%r+{!7^Ygl!tYjA}+KgozzP9!LNM>vfd27cQ@?KS#t6%h^h9Nxny{%Ppo@KQGX5#I{@t{I@5^PfXSe{CLqwQzF5zY*$R'
        'Lo$0krn?sx+h^Pv5$Q-zwS#M8#dM%qaR@buil>j7o}6AVK<ryPFUBcCZ%3+BD3N&Ay4Q2Y7m>XPsPEU4#I$$ZI8oZ$>2jopyryP|'
        '{LtT5zaV4kJH@{C&?~aZ?;R7m5+~#SSZpdPQ;d}-k~l1W+8+o)%E>PbI753#q1rhme2`MA2|&eU%*unbb;#QLu+xHuNG9$R=PCQ='
        '5pfH27C}Ej8WO|E<Oq2_g3syapGcYWwDS>us2YKore$aE!J09;a(Po^WD@;d_L0&eO7#6gDpcNP`#)Npj=bBaHmag}m=UR23`xgw'
        'euSCY0<j+j@;(I?3F8#Cd9y?yj-|G|>{n(|bsGjs8iJK?GkiA7^F1czej{s3Ji7JmR14x3`2$Ly7fV6Ft&hl=ZD-_>z!~OiO`mwj'
        'eTkDmpCw2j^8Qt35iMnjf^B|mx!^xZv093l&$PafL;#R2^Hy{7@Mbfq)?~F0E4n};?x40hF&y)g!tKo^27M!fWbNvEmdn=p_-K&>'
        '860#O)_EZ|teBi9r;jW*SzBjrwE^`p=IKcZYi^Y9RtOZd;8qs-pc|%yq1#siUQtG|vKmtdysC>b7EGH2I1W|9Q1sdLG_eCic!J`S'
        'z<fUwe-bVL!#0aR_&Jo>ukjC(L5y*;kPNw)_-^FD<L3ae`{934eix&cSv%j@(>iDM(vf3KKR0KZr#9;=rJ9-*jJC6EyfA4i-Z<|c'
        'eGB>bC)`W2aKjtG*RvWw1#iX6yQM0*G-^dQEw5<2)FexO{*vMzVn@u=m?Oe8QbL$8;=PHXsH(8QJ9H9%)1OrG?xuit7{?3$l&rN%'
        'l4+zW^Lz4J%17BwkrtztHO9=?@t0+|kO~@!yKl=O!o_z;i~^Og3+mg=FSSomtBkAlS-2GMtt9wjt1qb!gKocx5_#jZ(o1)I<lyhw'
        '6F}aHwH*JoO_LoGbkxA+UBzS@sxbIAny{H&5oT$=Ru0cgk7+Ey*?tzPVSd9ErqNsoEqe<6!Lp%U!?AFMSm5Xep}lar9#iMVo2mG~'
        'M-i(jyNK*r#`9u+tdh^2jA~8BNgtDVudB_T;RYW{klkiev(VS)jS~#m;wuf*GQq$ySa^G^5V^r=h}{cWeYamTf+jQTj>3v8Yc2C-'
        'p(<>H@g42OpA-4DtdDK%|H@|Rb7F~S95fR$L-2rAMhig}XhSTm%qRUEQbn^BXNW)W2ddfNjQYb4${|j6(RZY(dP4E*5%(4<FA~YD'
        'ketyER}a}yvt=dmZ-`5L1^RE+ZIzo~41DO)R>XzQ(;%GyBCljlz)|C|6Aw%ZHT+VUd9!7IXdQoA4{hk89%bA)0he=Dva-j}_-ls@'
        'v!kvUWgdGfin67U#3MZktEe1N+}BqRn%&E}2J!8hCXuE6T{cuKkzIEs(xigGbD#&@@wH@v#}W0R1l9{}TrQ4c8;qU<XLO#;y8o!B'
        '9xJikgIr0V4}$8BAe_e=4nG;Rlqx4&G>&yZrMLDjG$-98Fqs>G|C?2&)olCsF)6k_ZuCO|N$_8sct`Xa8q_{T;1;T1`*(m;p^)dK'
        'm1qSG{qW$!5B1z<#dbcz8TTvy*))r4DK>|-5D^VG&_o<!yC{Nt<B_2MIicw7)Z-7T<MO^f!TTg((kiUpw!JBDjaHgB>53&n7-obl'
        '17t*X;QoyO);!j=kQzAS{pBftpb=S?zYKIWb@os3@$2DMb6q+*dYqM&i}4;0rAz`=a{jZ8CHy_@{8k#QgDr+f{!Vj~`xFtsM){ok'
        '8Z3)`csp9(26ad~Q5pg&+j&S(vRst<ojrzoLo{%DK2m-YJ^B@M9C<Oo7~{n5^#?zZ&p1)i%X3Dmm%lTd=3Eq{<|I>RkCHvzkBe;*'
        'ujIW!I8jW@f0E}#m)6@J?J-LvDj*=Y3&6%|GZwtY=Bp&eknn&u6evS{|5G;Z*7D-R6prDy^fHg*c>$fo+6RYGblE&OY0<%SgymtV'
        'J<5$cl7?IC9e$b=05ohsQg9zSg|dTnJ;qADr5U!F*8<IVWGZ;37_4&iLjeaq>S)<wV+BUKfED7BS_g{{Ba`dILw;S2B{MefrYJ-p'
        '0WBqm%QY#Yl=Ih>6=JY&m0h<&G)mKE)8l9@{6ykun9qXwDbwd=+pz*$uFz=!uYh+O9}Yj4TVFzs<kH}5X$?7S)j=5Jw|NgoZ4xA9'
        '*^}S=spw0T+Ona=`7=DDqqXcCIh18i)TV2HSe8(y+)bjX`z&<o5dIyIx%zh5Lh{Pd(1k`ZCfVwqlyRwAeC*#j2dWy#N=;8LiCtR^'
        ';6#xd&Cnh>Toy*4zN1gCM^%5Xip2kEw1{rnJcG87k`OK;eV(C#pNmrYfIVmloU2&iMAuM0G)6a?KGE-2=Sa@|1K!?vQS$2e1?dcr'
        'vtUo10Ti!#EsMj_>$mNkTzRV;H&K0I3n#C{+vY8@?`TXp4~=?!;=+s<@=n;eq7{TfcwtPwS-iub?ocjT8GXVo$>PL(lLk*QHp+zC'
        'l?g9YaFDv4+{O=A40P^a<(QS$@FSj0w(~<nryh$cKNr6PSH+~imH&g{VC%amWc{KyEH-fb$MuSuZ6<4}gQ)<T=C0_o<!x)NFNHR&'
        'gVb~gU9V!%2dw{)%L!;7dw2VVMRI56a(V%cm8|8!>TBd+y9+1S|JZM_M(P*jJYU}pIQQRVN3W@{hS}_j=JY%E#_(#1O4g$Tk@Y+E'
        '+dA4Qh`N5&W|<nEXo;G3Z;4GR0X&Ubhvrj>lc{|>Xq9+C%H-~ip0;JvlOBp&gc#&XA7R4c-Wo*TB9{gYw5hD%;vw;NTaA#xC9fv5'
        '?*3ptr&})<IkHWAegGalH$h%4Kn4A84V?BXVh_C%H3pRSmtFgIl7FFpSuH$!?Z$RZT-sS?9e`Dt8dBfJq#<=f;u$IK&)C_fnV|nt'
        'XKPVW2i=DI>duo7k-}!H#rCW9te}eSc*K~cALQ?e8XNeOa+z3-{&~>SZXF2Rgw9p(M-b=NB`Y$N_GT(=nNBX-O?X?izbwI-rEF@a'
        '1BxWj2bJLt{Sd<P@42>oM{*RnOX9cN{OZ(5Twd(PoSIO%R-zL}2VSvPVo(KtxB+eU-{s&!myOi0ARfEk$L%=Y#|*^RxL}X7Ed_kY'
        'WT&FAb367gN-$^j3)T;*lda`nk2WGa$Qo8#@TgBfj8@h)t5-}53_MC3)xQzmip#zRO?U;elWESZ4t&v4AFY!F6XOuIWPJj90DVrb'
        '=w1C}v!m$`T#+SHus8CJ(AELQPvhdfze>-T8dl0rGe~itvePl%=0ZF5njW$u&XGSvocv6faW|MHL(`5!#t$EKF+&u$y}{WOkTWOA'
        'L=9$I%h(~_H7$I!|FCwygqX<-8vk8t;R;MwroPoODVCYqjxl=+!BINp9%Pw$r^{V88`!)fFsZ^q_3C41w_%qfAx#WNAf?yNKxoR1'
        ')`N_d|H{XuZ&Cns7boGLLc!s{{MU3;h-HQ(W<D$+mrB4_(RX6>G-KK8F*gYiHep1r!$83sIK{o+51fRH&CmK@MxS8Og$}+UY$~s#'
        'z$7oAJ0q|3(6;#NcduV`_E<jlUOAe-qB=X%bn*!JT<ls_DeQtJpZoSQ?QMd5)f1CN#~Yl@h?Tj<y@vN5NZRT-w)&ky)gHV7UBQZd'
        'yMlBNps$K+!G-XTk!PiWwQ@m8?1O&+Am_2&Nv#3Tp5tR%x%{Xt{ZK1wm*S7z$`-lEC->YW3=fUVDxxNWJ2zD&vYvkvX1Jr~e=Z!|'
        'fzGN`Iw%y{iTRUm5+(Wi51gWn*eTF)l6=7hsh@>$u1jt!BIHZ?5PDPkYe%@;xn)wCRKNk1OWBnw^!s13EMLYOG%n>u1g5@ugMm1a'
        'cE#wNa=Px9gsH#j4#DfBO9*m&1uBMjmtT0!)Zoux2Wrczq94%uww}cdF>$Hy`F5wYqYI6C^Ib`JENV8FN>e6tn}|s%NsE?+VQdsy'
        'FVl0mcUk+~s4QxBTTbU@^HkSCoEVnsFo$>yVa@!b=puUH_9nA)l{^PB+KE2Hiw9xGvnuV)Aj<85Ha?`rj-J=ZKMJYNv6B?-PYxHp'
        'nOEWvXW90z{Bg#)-|xay*?ZD_diD8f_ZcGnX8iEDk!UwfKQl?}UWBo^jU>w95cJc_?(c2#O5i)IN|KaI(e#>`Z~2}=7FpUf9L1+;'
        'GWn0O|J7MX0)b}@_7Ay4T-#x+eTxROK?!|o2zx9TA6VXtg(2#Tx>;weZ(Htq;HmxHvER(^-}J6oT(D^NMKTD}gJdGX>tyS9Elq>;'
        'lz*_kW}GDEc^VBfJvoXp`cw}fKW<JCsBpF_4TcN`?!RQ8yVxT=O)G_&ft)MoKMC5N_qqSwhG5ECFU5%ENCu6$A5f{j9&(^_@>nPs'
        '$yUsp+H~FLzEHT4)p;m<znQvKbDJNPMkRjWxpp5pC{>SQg1Jg<PM;Xd%1uRa?fBBZ48asm)S(P`4;f(T!fq{JP&qL;3@jSs8U0%n'
        'c<vO|4~}_*U!nNL;r{vXpJrQxy<7ctg^+wd6Ox58X`~lkU3S-WzuDY#Qh2z?Z_s`*$*G0HtHhigV{QjKb&&yx!`17g&-q&?r!1!|'
        'C@>*j66!s6+ovYjCPeY$xwFWT!|lp(^zP|vY1z5fehJ^h9ks{iKv6RF+^`h_{Kbt`$j`_@3+vopUzV){Z>xC0to9>UlBwed;mp6l'
        '19X^F;@J<U%pQ6iV`Fh$Eq6gO7zOEu?7*bHA-U7x$}?F7sSUF=`}ly|-x^qTMd(a5&pZ`}{{lo8f&u0Cbmy%RgND2sj!z`Q2&}BN'
        'jBb#W$&uAbv8xTNknWD!)ncei#A{v>%^v9e^?%l*IK0|A_-s`86t!|m#xpQYOykH;q<Ys-LD3L>wJijmOnCo-f9!lPRw2*Mmpyr2'
        '38Q2QjK$Jio!bEY*w{tH(-Y2yFLi6TCoT(^kTr(Kg-~v1{_N0OyA@1zCwq6qww{C#dz6)KJx6P`qrD{HVVS3Rcod3e2(o>$vOQ(c'
        'EwelfQ;dz?HvIeM0K~+$;`IdS`Q+~x0IHNTZNMF-r-5nL{?y?5jxm8Ky$gLg{@w1}J|A{CqDdj&f)K&6+p3h7+Q)FS(yTJB4>+VW'
        'Ql3{=xMpqguzG&`Ew@|ztlgQ4=yKB)?}#0wu_GZ7h_S{Dut3LwkbQ>c;Y64APi~0l<&(2KoSl;=Z9Ml1y@43K@P}@U*w@NZ%pCxA'
        'vqTvycRCW==#zNfO2#1=5Yv<~01%04l*tb$|KVz$5mDKI&iPtYuV%4q7g-B5+&xXwiU=V>C2O|g#@RR$Tiyq!Np$3zSRnDpjT;o)'
        '2d+~$i{bl0p+Sr&#GA!yOLYa^8L}q!q{aVmNI#2%DL_-IPXYJQIL={eb;2QXud3$(MhsU}hJVZTf&A{}Nf}Xd^iY*yVGGZzM>ozq'
        '%Ezt2e+i>HAkCr$)LMqZR);y#xvrt8cVrt``YVdc?T`)aKT_$dd-^Yb6L1HHtrmS*(^6;h{UPJ61|de&F<RysZhP?X2I0p})csyF'
        '1utQrZ!p4=<$X=;#`$$#VI3-%C|s(w5PwMMh=Su*E%zUm$r@$Eqw0R&F~3k^ck`T*Z~w^}Uc=H{Fy;rmwNOnQOwUXWMp&j%YCl~1'
        'ugCJAHxh}`6vz`PNKzM3w(G(T6*=B~+HR@1jsBv|A@zck_E7<F^J^E1ykn%m3v1dP&#9<4L8pot#Tht4;N&DjVFtj-p%irZOi=3-'
        'kRwOXdf?xiHwCB=qk$_nYq;>;4U}CCX#!Njxj($bE{+2^+}f-s?h*Jy-L(W=6w&<dF`uhp-M{C)oDlUC(1po|)g&h`fb|0XuG|jd'
        '6Nc075h9oK`G|j9dI;GPdof<!QU$0B()76>(DnHxnYCq-c58JH3TdPpUV7YhC;I7<VR*bC6m>*4Iv^skPs5g>FRek(xQO46r=0&_'
        's-F;($PcR{M`)Vs{~7~Z$H{&Rrpt)DDbvkj*PD?YNnX)51J5(R|8}5iIaAAXuc&Tqp~vgy(|O0^pAWL~e~(Jje$E+Vp*~zYKjh3`'
        'It8Wt5?TD-%fDhHicZU(uwZv80oHPcl}YciF<t={FT!gIM2AkE3hk_RfD1waN04vMJc!~3qI^tp9`d79ie?<pA2WR;C^u<D?gN(4'
        'PqOvF|4zEk_kJVm3sNi%@a64Kg=MrZS?;w{vCenn^|a~!#J}W$t<Xg75ET$GgW->OLBE8+`{>J}h~k!;Feo;8&Oh;W4~Hw!6oX`w'
        'HVHK<OIF15dgc3{6-dvx?+RM0AHx9YEvL9W-$wChJw+4cj79ow3}nGCSA=HPCPV_^ohsbH<bhT-!ldmQp7dtn9E7Y<)vR7mJH0(t'
        'I>)~r7uBC0XJvYG<y`o|9z8;4#26li+SYbM9B}y)+k|jDs?xU;M@ng(%vimg_B-Y`2Q3}nd?=T{i<P^(PN8$=bSQ42X!Rfe=c5|5'
        'x}H)c{`s2X1v!4gS!yQ_8sOeUC;_&4))w%;h60cez+P(bemas+)%$f}MDf1t*^>QNl4FjSY#JrPxxe4G^$YCLFSaRwIz2K5y(Bw^'
        'XvC^-?6WdMRD8|L9ysWs0wl$A<paF1yf_7Xb?gyp$4|R)NzP)nIleRpzQXeaSR${d@;w|b!cTi4FPICy@M4I)+1D|CyM~Cw=DpTC'
        'xuJZKDra-SNEnbXz=!>Q$COZXdLN?z4k$X#f0{O=j)BN?GUS}MuLmEri>PWWi)~b?=b018Not;1<T>hs{#>s}n@cfFRB-=h%xP*H'
        'V(5VINhP%O@RJ)+BorSi(R`wCh9v+w7~G8Kl@(O><f6Zd4ik1LQTMK!<uw|oag-~3<p2&Btd~A9%)V!szz36vi?BH|*UgS{Pp`!a'
        'CG|0{>J~3P#rRCJn<tPq9Wu=(@Mvd(BRk&;m^*q@sZ(0~kOhq4^|t$am4(FLEu;zj64chiG9TQWp<aK4q-4g|*kyiy@ev-raU8hu'
        'aYFN<U@JZ=^*qyxu^JyrJ|GB!b`aL|9@<bTnV}Qj{Iea0&0Nxr{Y$dVsV0cHB9mvKu*8gj*3KE+{b+V|Y0L^b<pvCJVn+Gv;4QRG'
        '{THTD1*0GRY}^nD%d%j)=`5Cf!b#^DD?GmM=tc)F(T0Mju)_pGK;MM=vA4R8_e8(?b!v#q6S+Us!fCZqUU;GUqFS~Cxx2*kFL`dM'
        '=#U*5lMUw*pAQh~c(`)9iR>MeQ(GxmQonV^WV7+!{^kap;s_?DJv{rC>%@yv1l{MFXvB75Qc!Wa<ECvi9FsPy>u?X3WSXDn5jZR~'
        '41Pn${=y%LmpyOjCe+_d-&5+?F|#>N=LH4<wir@Vzak#L)i?A<@Nk3m2@$;9rU2#ndUUHhI{9Grid_bZg*X2Eb83}0y3dGL`vGh+'
        'x$W-962kv#tvFKVy8(T$s?Uw|G19Fu$O1Hwb#=~&;O|>uBEhjDdA6Uc8tf4{4}|LH-xa!n#<zZ~>=#~?YcP&KOarZN%QwAw1oqff'
        'lHq5qlNlkX`VIp^?@FV$?uABp+lW}uSa8FQ;r93|Xn9TV5oZtZNK7=o%joV5U<Fxnx)DEk5fjUD;yO)ZX@o#aT9nLMg$FIN6VBJr'
        '&>hs;eaW6t!rmB?u%YI>DW9{K3vDdZC}r2X#0SF;Q%h#l{ZT5j1hTGb>Ox$TT|hPME4;Zk`qdY5F%*Q+VPLIww43Q>_bcN%f~PWc'
        'GxUM{Gd^B+w|;%n4$nIK8N75o^jePfV2e)EY2htiIja9RArXXlLSt-Yo8cM{tbFEF1JqTtmFpF4BmWP;&;#r'
    ),
}


TOAST_SCRIPT = r'''
param([int]$Count = 1, [int]$TestAlert = 0, [switch]$FormatOnly)
$ErrorActionPreference = 'Stop'
$noun = if ($Count -eq 1) { 'ticket' } else { 'tickets' }
$body = "$Count new E621 moderator $noun found"
if ($FormatOnly) { Write-Output $body; exit 0 }
try {
    $appId = 'Wisp.E621TicketBot'
    $reg = 'HKCU:\Software\Classes\AppUserModelId\' + $appId
    New-Item -Path $reg -Force | Out-Null
    New-ItemProperty -Path $reg -Name 'DisplayName' -Value 'E621 Ticket Bot' -PropertyType String -Force | Out-Null
    $iconPath = Join-Path $PSScriptRoot 'assets\e621-ticket-bot.png'
    New-ItemProperty -Path $reg -Name 'IconUri' -Value $iconPath -PropertyType String -Force | Out-Null
    $iconUri = [System.Security.SecurityElement]::Escape(([System.Uri]::new($iconPath)).AbsoluteUri)
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $title = 'E621 Ticket Bot'
    if ($TestAlert -eq 1) { $title = 'E621 Ticket Bot - test'; $body = "Test alert: $Count ticket(s). No API request was made." }
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><image placement='appLogoOverride' src='$iconUri'/><text>$title</text><text>$body</text></binding></visual><audio silent='true'/></toast>")
    $notification = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notification.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(5)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($notification)
    Start-Sleep -Milliseconds 300
    exit 0
} catch {
    # Avoid emitting raw system or request objects into logs.
    Write-Error 'Windows toast submission failed.' -ErrorAction Continue
    exit 1
}
'''


if __name__ == "__main__":
    raise SystemExit(main())
