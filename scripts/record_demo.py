"""Record the demo video: the whole job on the web screen, driven like a person would.

Usage (server already running: `python web/server.py --debug`, keys in the environment):
    python scripts/record_demo.py [--base http://127.0.0.1:8000] [--out recordings/demo.mp4]
Needs `pip install playwright imageio-ffmpeg` and Google Chrome.

What it shows, in order: the landing page; the host typing the PARC description; the brief,
the host check and the ranked sponsors with their sources; the operator view (weights);
picking four companies; the verified decision-makers and drafted pitches; approving and
sending (to the sandbox outbox); two sponsors replying (typed by a teammate playing them,
as the handbook plans for the demo); the agent answering from the brief and booking a
meeting, each send approved on screen; and the pipeline.

The captions say what is live and what is played: the search itself replays the live run
made earlier (same words, same run), and sponsor replies are typed by a teammate.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DESCRIPTION = (ROOT / "demo" / "parc_health_camp.txt").read_text(encoding="utf-8").strip()
PICKS = ["RBL Bank", "Sahyadri Hospitals", "Apollo Tyres Ltd", "Ather Energy"]
REPLIES = [
    ("RBL Bank", "Thanks for thinking of us. Our CSR budget for this year is already committed, "
                 "so we'll have to pass this time."),
    ("Sahyadri Hospitals", "Thanks for reaching out! How many people do you expect at the camp, "
                           "and which checkups will you offer?"),
]
FOLLOW_UP = ("Sahyadri Hospitals", "That's a great fit for us. Let's set up a call to talk about partnering.")

OVERLAY = """
(() => {
  if (document.getElementById('demo-cursor')) return;
  const style = document.createElement('style');
  style.textContent = `
    #demo-cursor { position: fixed; z-index: 99999; left: 0; top: 0; width: 22px; height: 22px; pointer-events: none;
      transform: translate(-100px, -100px); transition: transform .7s cubic-bezier(.2,.8,.2,1); }
    #demo-cursor svg { width: 22px; height: 22px; filter: drop-shadow(1px 2px 2px rgba(0,0,0,.35)); }
    #demo-cursor.click::after { content: ''; position: absolute; left: -9px; top: -9px; width: 40px; height: 40px;
      border-radius: 50%; border: 3px solid #2d62ff; animation: demo-ring .45s ease-out forwards; }
    @keyframes demo-ring { from { transform: scale(.3); opacity: 1; } to { transform: scale(1); opacity: 0; } }
    #demo-caption { position: fixed; z-index: 99998; left: 50%; bottom: 112px; transform: translateX(-50%);
      max-width: min(980px, 92vw); padding: 14px 22px; border-radius: 14px; background: #111; color: #fff;
      font: 500 19px/1.4 "DM Sans", system-ui, sans-serif; box-shadow: 0 12px 30px rgba(0,0,0,.25);
      border: 2px solid #111; opacity: 0; transition: opacity .35s; text-align: center; }
    #demo-caption b { color: #fecc4c; font-weight: 700; }
    #demo-caption small { display: block; color: #c9c8be; font-size: 14px; margin-top: 2px; }
    #demo-title { position: fixed; inset: 0; z-index: 100000; display: grid; place-items: center; background: #f3f2e9;
      transition: opacity .6s; text-align: center; }
    #demo-title h1 { font: 500 76px/1 "Sentient", Georgia, serif; margin: 0; letter-spacing: -.02em; }
    #demo-title h1 em { color: #18b286; }
    #demo-title p { font: 500 22px "DM Sans", sans-serif; color: #3d3d3a; margin: 18px 0 0; }`;
  document.head.append(style);
  const cursor = document.createElement('div');
  cursor.id = 'demo-cursor';
  cursor.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3 2l17 9-7.5 1.8L9 20z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
  const caption = document.createElement('div');
  caption.id = 'demo-caption';
  document.body.append(cursor, caption);
})()
"""


class Demo:
    def __init__(self, page: Page):
        self.page = page

    def overlay(self) -> None:
        self.page.evaluate(OVERLAY)

    def caption(self, text: str, small: str = "", hold: float = 0.0) -> None:
        self.page.evaluate("""([t, s]) => { const c = document.getElementById('demo-caption');
            c.innerHTML = ''; const main = document.createElement('span'); main.innerHTML = t; c.append(main);
            if (s) { const sm = document.createElement('small'); sm.textContent = s; c.append(sm); }
            c.style.opacity = t ? 1 : 0; }""", [text, small])
        time.sleep(hold)

    def title(self, heading: str, sub: str, hold: float = 3.0) -> None:
        self.page.evaluate("""([h, s]) => { const d = document.createElement('div'); d.id = 'demo-title';
            d.innerHTML = '<div><h1>' + h + '</h1><p>' + s + '</p></div>'; document.body.append(d); }""", [heading, sub])
        time.sleep(hold)
        self.page.evaluate("() => { const d = document.getElementById('demo-title'); d.style.opacity = 0; setTimeout(() => d.remove(), 700); }")
        time.sleep(0.8)

    def move_to(self, locator) -> None:
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box:
            x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
            self.page.evaluate("([x, y]) => document.getElementById('demo-cursor').style.transform = `translate(${x}px, ${y}px)`", [x, y])
            time.sleep(0.8)

    def click(self, locator, pause: float = 0.6) -> None:
        self.move_to(locator)
        self.page.evaluate("() => { const c = document.getElementById('demo-cursor'); c.classList.remove('click'); void c.offsetWidth; c.classList.add('click'); }")
        locator.click()
        time.sleep(pause)

    def scroll_to(self, selector: str, pause: float = 1.4, block: str = "start") -> None:
        self.page.evaluate("([s, b]) => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: b})", [selector, block])
        time.sleep(pause)

    def scroll_by(self, dy: int, pause: float = 1.2) -> None:
        self.page.evaluate("dy => window.scrollBy({top: dy, behavior: 'smooth'})", dy)
        time.sleep(pause)

    def wait_for(self, js: str, timeout: float = 240) -> None:
        self.page.wait_for_function(js, timeout=timeout * 1000)

    def wait_idle(self, timeout: float = 240) -> None:
        """Until the agent has no job running (the status line hides)."""
        self.page.wait_for_function("() => document.getElementById('status').hidden", timeout=timeout * 1000)
        time.sleep(0.8)

    def approve_dialog(self, caption: str, small: str = "") -> None:
        self.page.wait_for_selector("#approval[open]", timeout=180_000)
        time.sleep(1.0)
        self.caption(caption, small, hold=4.5)
        self.click(self.page.locator("#approval-yes"), pause=1.2)

    def card(self, name: str):
        return self.page.locator("#sponsor-list li.sponsor", has=self.page.locator(".sponsor-name", has_text=name)).first


def record(base: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.parent / "raw"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                      record_video_dir=str(raw), record_video_size={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())       # the "Send 4 emails?" confirm
        page.goto(base + "/", wait_until="networkidle")
        demo = Demo(page)
        demo.overlay()

        # -- 0. the site ----------------------------------------------------------------
        demo.title("Sponsor <em>Agent</em>", "Finds, checks and books sponsors for your event · ContextCon 05 demo")
        demo.caption("An event host opens <b>Sponsor Agent</b>.", hold=3.0)
        demo.scroll_to("#how", pause=1.6)
        demo.caption("Four steps: describe the event, <b>verify the host</b>, find sourced sponsors, <b>approve every pitch</b>.", hold=4.0)

        # -- 1. describe the event -----------------------------------------------------
        demo.scroll_to("#plan", pause=1.6)
        demo.caption("<b>1 · Describe the event</b> in plain words.", "PARC Health Camp: a real NGO at BITS Pilani")
        demo.click(page.locator("#description"), pause=0.3)
        page.locator("#description").press_sequentially(DESCRIPTION, delay=9)
        time.sleep(1.0)
        demo.click(page.locator("#run-btn"), pause=0.5)
        demo.caption("<b>2 · The agent reads the brief, checks the host and searches.</b>",
                     "This search ran live earlier today (79 credits); the same words replay it at no cost.", hold=0.5)
        demo.wait_for("() => document.querySelectorAll('#sponsor-list li.sponsor').length > 0")
        demo.wait_idle()

        # -- 2. brief + host -----------------------------------------------------------------
        demo.scroll_to("#brief-card", pause=1.4)
        demo.caption("The brief keeps <b>only what the host actually wrote</b>; anything else is left out and listed.", hold=4.0)
        demo.move_to(page.locator("#host-body"))
        demo.caption("<b>Host check:</b> PARC's own website (atmanirbhar.org) names PARC, so outreach may go ahead.",
                     "This stops anyone using the agent to pose as another organisation.", hold=4.5)

        # -- 3. sponsors ---------------------------------------------------------------------
        demo.scroll_to("#sponsors", pause=1.6)
        demo.caption("<b>3 · 55 companies found</b>, ranked only by facts with a source and a date.", hold=3.5)
        rbl = demo.card("RBL Bank")
        demo.click(rbl.locator(".facts-toggle"), pause=0.8)
        demo.caption("Is this real? Each reason links to its source: here a <b>LinkedIn post</b> naming RBL Bank as a health-camp sponsor.",
                     "Facts we can't verify are shown as such, never as true.", hold=5.5)
        demo.click(rbl.locator(".facts-toggle"), pause=0.5)
        demo.scroll_by(420, pause=1.4)
        demo.caption("Logos come from the company records; every card says <b>how it was found</b>.", hold=3.5)

        # operator view
        demo.scroll_to("#brief-card", pause=1.0)
        demo.click(page.locator("#debug-switch"), pause=1.2)
        demo.scroll_to("#debug-panel", pause=1.2)
        demo.caption("<b>Operator view</b> (only on a --debug server): the weights for an NGO host and the cost so far.", hold=5.0)
        demo.scroll_to("#sponsor-list", pause=1.2)
        demo.caption("Rank = score × confidence: what's sourced counts; unknowns earn nothing and are never stored as zero.", hold=5.0)
        demo.click(page.locator("#debug-switch"), pause=1.0)

        # -- 4. pick ------------------------------------------------------------------------
        demo.caption("<b>4 · The host picks who to pitch.</b>", hold=1.0)
        for name in PICKS:
            card = demo.card(name)
            card.scroll_into_view_if_needed()
            time.sleep(0.5)
            demo.click(card.locator("label.pick"), pause=0.7)
        demo.caption("Contacts are only looked up for picked companies: each lookup costs credits.", hold=2.5)
        demo.click(page.locator("#draft-btn"), pause=0.5)
        demo.caption("Finding each company's decision-maker and drafting the pitch from the brief and the sourced facts…", hold=0.5)
        demo.wait_for(f"() => document.querySelectorAll('#pitch-list .pitch').length >= {len(PICKS)}")
        demo.wait_idle()

        # -- 5. pitches -------------------------------------------------------------------
        demo.scroll_to("#pitches", pause=1.6)
        demo.caption("<b>5 · Verified people:</b> each contact holds that role at the company now, with the source and date.", hold=5.0)
        demo.caption("Every number in a pitch must already be in the brief or the evidence, or the draft is dropped.", hold=4.0)
        for box in page.locator("#pitch-list .pitch label.pick").all():
            demo.click(box, pause=0.6)
        demo.caption("<b>Nothing is sent until the host approves each pitch.</b>", hold=2.0)
        demo.click(page.locator("#send-btn"), pause=0.5)
        demo.caption("Sending 4 approved pitches, each exactly as shown and at most once…", hold=0.5)
        demo.wait_for(f"() => document.querySelectorAll('#threads .msg').length >= {len(PICKS)}")
        demo.wait_idle()

        # -- 6. replies ---------------------------------------------------------------------
        demo.scroll_to("#mailbox", pause=1.6)
        demo.caption("<b>Sandbox:</b> nothing leaves this machine. The 4 emails are saved locally.",
                     "During the event, agents only email our own team. A teammate plays the sponsors.", hold=5.0)
        for company, text in REPLIES:
            demo.caption(f"<b>6 · {company} replies</b> (typed by a teammate playing the sponsor).", hold=0.5)
            select = page.locator("#sponsor-company")
            demo.move_to(select)
            select.select_option(label=company)
            time.sleep(0.6)
            demo.click(page.locator("#sponsor-text"), pause=0.2)
            page.locator("#sponsor-text").press_sequentially(text, delay=14)
            time.sleep(0.6)
            demo.click(page.locator("#sponsor-send"), pause=1.5)
        demo.wait_for(f"() => document.querySelectorAll('#threads .msg-in').length >= {len(REPLIES)}")
        demo.caption("The agent reads each reply and decides the next step.", hold=1.0)
        demo.click(page.locator("#replies-btn"), pause=0.5)
        demo.approve_dialog("Sahyadri asked a question: the agent <b>answers only from the brief</b>, and asks the host first.",
                            "RBL Bank said no: marked declined, never pitched again for this event.")
        demo.wait_idle()
        demo.scroll_to("#threads", pause=1.4)
        demo.caption("The answer is in the thread, and RBL Bank is marked declined.", hold=4.0)

        # -- 7. meeting ---------------------------------------------------------------------
        company, text = FOLLOW_UP
        demo.caption(f"<b>7 · {company} wants to talk.</b>", hold=0.5)
        select = page.locator("#sponsor-company")
        demo.move_to(select)
        select.select_option(label=company)
        demo.click(page.locator("#sponsor-text"), pause=0.2)
        page.locator("#sponsor-text").press_sequentially(text, delay=14)
        time.sleep(0.5)
        demo.click(page.locator("#sponsor-send"), pause=1.5)
        demo.wait_for(f"() => document.querySelectorAll('#threads .msg-in').length >= {len(REPLIES) + 1}")
        demo.click(page.locator("#replies-btn"), pause=0.5)
        demo.approve_dialog("Interested → the agent proposes a <b>calendar invite</b> for the next free slot. The host approves.")
        demo.wait_idle()
        demo.scroll_to("#threads", pause=1.2, block="end")
        demo.caption("Invite sent with an .ics attachment: <b>meeting booked</b>.", hold=4.0)

        # -- 8. pipeline ----------------------------------------------------------------------
        demo.scroll_to("#pipeline", pause=1.6)
        demo.caption("<b>8 · The finished result:</b> every company's state and why. Negotiation always goes to the host.",
                     "No reply from Apollo Tyres and Ather: up to 2 follow-ups after 3 days, then no response.", hold=6.0)
        demo.scroll_to("#brief-card", pause=1.0)
        demo.click(page.locator("#debug-switch"), pause=1.0)
        demo.scroll_to("#debug-panel", pause=1.2)
        demo.caption("And the <b>cost of the whole job</b>, from the ledger: every Crustdata and model call is recorded.", hold=5.0)
        demo.caption("")
        demo.title("Sponsors, <em>found, checked and booked.</em>", "Every fact sourced · every email approved · nothing sent twice", hold=3.5)

        video = page.video.path()
        context.close()
        browser.close()

    ffmpeg = __import__("imageio_ffmpeg").get_ffmpeg_exe()
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "20", "-movflags", "+faststart", str(out)], check=True)
    print(f"Saved {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default=str(ROOT / "recordings" / "demo.mp4"))
    args = parser.parse_args()
    record(args.base, Path(args.out))


if __name__ == "__main__":
    main()
