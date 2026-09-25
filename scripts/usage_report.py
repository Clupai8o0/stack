#!/usr/bin/env python3
"""What your Claude Code accounts spent, read from their own transcripts. No API key, no network.

    python3 usage_report.py [--days 7] [--write]

Prints spend at API list price (a stand-in for plan usage), the share spent on calls with more than 200k tokens of
context, sessions that grew past 200k and 400k, the subagent share, spend per model and per account, the start-up
context of a session and of a subagent, and any model the token-economy skill says not to use.

--write also keeps one row per run in the vault note 30-resources/claude-usage.md (a launchd job runs it on
Saturdays before the weekly review). Reads only; the transcripts are never changed.
"""
import argparse, collections, glob, json, os, re, statistics, sys, time

HOME = os.path.expanduser('~')
ACCOUNTS = {'.claude': 'main', '.claude-exec': 'cx'}
VAULT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))   # the repo root
NOTE = os.path.join(VAULT, 'claude-usage.md')
# $ per million tokens: input, output, cache read (list prices from Claude Code's bundled claude-api skill,
# shared/models.md, Sep 2026; Fable 5.1 and Opus 5.5 cache reads really are $0.25 and $0.20). A 5-minute cache write
# costs 1.25x input and a 1-hour one 2x.
PRICES = {'claude-fable-5-1': (10, 50, 0.25), 'claude-fable-5': (10, 50, 1.0), 'claude-opus-5-5': (4, 20, 0.20),
          'claude-opus-5': (5, 25, 0.5), 'claude-opus-4-8': (5, 25, 0.5), 'claude-opus-4-7': (5, 25, 0.5),
          'claude-sonnet-5': (2, 10, 0.2), 'claude-sonnet-4-6': (3, 15, 0.3), 'claude-haiku-4-5': (1, 5, 0.1)}
AVOID = {'claude-opus-4-8': 'worse and 2.3x the cost of Opus 5 low', 'claude-haiku-4-5': 'missed 62% of review defects'}
BIG, HUGE = 200_000, 400_000


def price(model):
    model = re.sub(r'-\d{8}$', '', model or '')          # claude-haiku-4-5-20251001 -> claude-haiku-4-5
    return PRICES.get(model) or PRICES['claude-opus-5'], model


def scan(days):
    since = time.time() - days * 86400
    since_iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(since))
    calls = {}                                                # message id -> (path, acct, kind, model, usage)
    for cfg, acct in ACCOUNTS.items():
        for path in glob.glob(os.path.join(HOME, cfg, 'projects', '**', '*.jsonl'), recursive=True):
            try:
                if os.path.getmtime(path) < since:
                    continue                                  # nothing in it is recent
                fh = open(path, encoding='utf-8', errors='replace')
            except OSError:
                continue
            sub = '/subagents/' in path
            with fh:
                for line in fh:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get('message') if isinstance(d.get('message'), dict) else {}
                    u = m.get('usage')
                    if d.get('type') != 'assistant' or not isinstance(u, dict) or (d.get('timestamp') or '') < since_iso:
                        continue
                    if m.get('model') == '<synthetic>':
                        continue
                    # One API response is logged once per content block, and only the last line has the final
                    # output_tokens (earlier ones carry a partial count), so keep the line with the most output.
                    mid = m.get('id') or d.get('uuid')
                    old = calls.get(mid)
                    if old is None:
                        calls[mid] = (path, acct, 'sub' if sub or d.get('isSidechain') else 'main', m.get('model'), u)
                    elif (u.get('output_tokens') or 0) >= (old[4].get('output_tokens') or 0):
                        calls[mid] = old[:4] + (u,)
    first, peak = {}, {}
    spend = collections.Counter()
    unknown = set()
    for path, acct, kind, raw, u in calls.values():          # first-seen order, so first[path] is its first call
        (pin, pout, pcr), model = price(raw)
        if model not in PRICES:
            unknown.add(model)
        cc = u.get('cache_creation') if isinstance(u.get('cache_creation'), dict) else {}
        w1h = cc.get('ephemeral_1h_input_tokens') or 0
        w5m = cc.get('ephemeral_5m_input_tokens') or (0 if cc else u.get('cache_creation_input_tokens') or 0)
        inp, cr, out = u.get('input_tokens') or 0, u.get('cache_read_input_tokens') or 0, u.get('output_tokens') or 0
        usd = (inp * pin + w5m * pin * 1.25 + w1h * pin * 2 + cr * pcr + out * pout) / 1e6
        ctx = inp + cr + w1h + w5m
        spend['all'] += usd
        spend['kind:' + kind] += usd
        spend['model:' + model] += usd
        spend['acct:' + acct] += usd
        if ctx >= BIG:
            spend['big'] += usd
        first.setdefault(path, (kind, ctx))
        if kind == 'main':
            peak[path] = max(peak.get(path, 0), ctx)
    starts = {k: [c for kk, c in first.values() if kk == k] for k in ('main', 'sub')}
    return dict(since=since, spend=spend, peak=peak, starts=starts, unknown=unknown)


def pct(a, b):
    return f'{a / b * 100:.0f}%' if b else '-'


def report(r, days):
    s, total = r['spend'], r['spend']['all']
    peaks = list(r['peak'].values())
    models = sorted(((k[6:], v) for k, v in s.items() if k.startswith('model:')), key=lambda x: -x[1])
    lines = [f'Claude usage, last {days} days (3 accounts, API list price, a stand-in for plan usage)',
             f'- Spend: ${total:,.0f} (main sessions {pct(s["kind:main"], total)}, subagents {pct(s["kind:sub"], total)})',
             f'- Spent on calls past 200k context: {pct(s["big"], total)}'
             f' | sessions past 200k: {sum(p >= BIG for p in peaks)} of {len(peaks)}, past 400k: {sum(p >= HUGE for p in peaks)}',
             '- By model: ' + ', '.join(f'{m} ${v:,.0f} ({pct(v, total)})' for m, v in models if v >= total * 0.005),
             '- By account: ' + ', '.join(f'{a} {pct(s["acct:" + a], total)}' for a in ACCOUNTS.values())]
    med = {k: statistics.median(v) / 1000 for k, v in r['starts'].items() if v}
    if med:
        lines.append('- Start-up context: ' + ', '.join(f'{"session" if k == "main" else "subagent"} {v:.0f}k'
                                                        for k, v in med.items()))
    flags = [f'{m} got {pct(v, total)} ({AVOID[m]})' for m, v in models if m in AVOID and v >= total * 0.005]
    flags += [f'unpriced model {m} counted at Opus 5 prices' for m in sorted(r['unknown'])]
    if flags:
        lines.append('- Check: ' + '; '.join(flags))
    return lines


def write_note(r, days):
    s, total = r['spend'], r['spend']['all']
    peaks = list(r['peak'].values())
    top = max(((k[6:], v) for k, v in s.items() if k.startswith('model:')), key=lambda x: x[1], default=('-', 0))
    today = time.strftime('%Y-%m-%d')
    row = (f'| {today} | {days} | ${total:,.0f} | {pct(s["big"], total)} | {sum(p >= BIG for p in peaks)}/{len(peaks)} '
           f'| {pct(s["kind:sub"], total)} | {top[0]} {pct(top[1], total)} |')
    head = ['---', 'id: 20260923-claude-usage', 'title: Claude usage by week', 'type: note', 'status: seedling',
            'tags: [ai, tools, routing]', 'created: 2026-09-23', f'updated: {today}',
            'related: ["[[30-resources/model-bakeoff-2026-09|model bake-off]]"]', '---', '',
            '# Claude usage by week', '',
            'One row per run of `usage_report.py --write` (Saturdays, launchd).',
            'Spend is API list price across all three accounts. "Past 200k" is the share spent on calls with more',
            'than 200k tokens of context; auto-compact at 250k (set 23 Sep 2026) should push it down.', '',
            '| Date | Days | Spend | Past 200k | Sessions past 200k | Subagents | Top model |',
            '|---|---|---|---|---|---|---|']
    try:
        lines = open(NOTE).read().splitlines()
    except OSError:
        lines = []
    is_row = re.compile(r'\|\s*\d{4}-\d\d-\d\d\s*\|').match
    rows = [i for i, l in enumerate(lines) if is_row(l)]
    if not rows:
        lines = head + [row]                                  # new note (or one with no table left)
    else:
        # Only the table changes: today's row is replaced, or the new row goes after the last one. Anything written
        # around the table is kept as it is.
        same = [i for i in rows if re.match(rf'\|\s*{today}\s*\|', lines[i])]
        if same:
            lines[same[0]] = row
            for i in reversed(same[1:]):
                del lines[i]
        else:
            lines.insert(rows[-1] + 1, row)
        fm_end = lines.index('---', 1) if lines[:1] == ['---'] and '---' in lines[1:] else 0
        lines = [f'updated: {today}' if i < fm_end and l.startswith('updated: ') else l for i, l in enumerate(lines)]
    tmp = f'{NOTE}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, NOTE)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--write', action='store_true', help=f'also add a row to {os.path.relpath(NOTE, VAULT)}')
    a = ap.parse_args()
    r = scan(a.days)
    print('\n'.join(report(r, a.days)))
    if a.write:
        print(f'wrote {os.path.relpath(NOTE, VAULT)}: {write_note(r, a.days)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
