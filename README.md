# Ditch Channel → Instagram show poster

Posts Instagram Stories automatically for every show on Ditch Channel:

- **Coming up**: a Story about an hour before each show
- **Live now**: a Story when the show starts
- **Live DJ, not on the schedule**: a "Live now" Story when someone goes live on Radio.co, once per set

**About links:** Instagram doesn't let automated Stories carry a link sticker (or any sticker). So every card says **LINK IN BIO · DITCH.CHANNEL**. Make sure ditch.channel is the link in your Instagram bio. You can change the wording with `CARD_LINK_TEXT`.
Want feed posts too? Set `COMING_UP_AS` / `LIVE_NOW_AS` to `feed` or `both`. Feed posts also get a caption (Stories don't have captions).

Each post gets a Ditch card: the wavy stamp and wordmark on the show's own Radio.co colour (1080×1920 for Stories, 1080×1350 if you turn feed posts on). Change a show's colour in Radio.co and its cards follow. Nothing is ever posted twice.

## Where the schedule comes from

1. **Radio.co.** The script reads the same feed your Radio.co schedule widget uses. This covers both prerecords and live slots, as long as they're in your Radio.co schedule.
   *Note: Radio.co doesn't officially document this feed. If it ever stops working, the script falls back to option 2.*
2. **`schedule.csv` (optional).** Use it to add shows Radio.co doesn't know about, or to fix a name or add a DJ. Weekly shows can use a day name (`Fri`) instead of a date.

The Jukebox (Radio.co playlist "All") is skipped by default. Add any other playlists to skip to `IGNORE_SHOWS`, comma-separated, e.g. `All,Filler`.

Run `python ditch_poster.py --list` to see exactly what it thinks is coming up.

---

## Setup (about 30 minutes, once)

### 1. Instagram access (the fiddly bit)
Instagram only allows automated posting through Meta's official API:

1. Switch your Instagram to a **Business account** (Settings → Account type and tools). It must be **Business**, not Creator: Instagram only allows posting Stories through the API from Business accounts.
2. Go to **developers.facebook.com** → *My Apps* → *Create app*. Pick the **Instagram API** use case.
3. In the app, add your Instagram account. Generate an **access token** with the `instagram_business_basic` and `instagram_business_content_publish` permissions.
4. Note your **Instagram user ID** (shown next to the token).

Tokens last 60 days. Meta lets you refresh a long-lived token before it expires, so put a reminder in your calendar.

### 2. Put it on GitHub (free, no server needed)
1. Create a **public** GitHub repo (e.g. `ditch-poster`) and upload everything in this folder, including the hidden `.github` folder.
   The repo has to be public because Instagram fetches the card images from it. Your token stays private in Secrets.
2. Go to repo **Settings → Secrets and variables → Actions**:
   - **Secrets:** `IG_USER_ID`, `IG_ACCESS_TOKEN`
   - **Variables (all optional):** `IGNORE_SHOWS`, `HASHTAGS`, `COMING_UP_MINUTES`, `COMING_UP_AS`, `LIVE_NOW_AS`. Your station ID (`sd72a8fdcd`) is already built in.
3. Go to the **Actions** tab and enable workflows. It runs every 10 minutes.

### 3. Test, then go live
- It starts in **dry-run mode**: it makes cards and writes captions, but posts nothing. Check the *Actions* run logs to see what it would have posted.
- When you're happy, add a variable `DRY_RUN` = `false`.

### Running it on your own machine instead
```
pip install -r requirements.txt
cp .env.example .env      # fill it in
python ditch_poster.py --preview   # see sample cards in ./out
python ditch_poster.py --dry-run   # a real pass, posting nothing
```
Then run `python ditch_poster.py` from cron every 5–10 minutes. On your own machine, set `IMAGE_HOST=folder` if you have a web folder to put the images in.

---

## Make it look like Ditch
- Card background = the show's colour in Radio.co (edit the playlist colour there)
- `assets/stamp.png` and `assets/wordmark.png` are your artwork; replace them to restyle
- `ACCENT_COLOUR` (fallback when a show has no colour), `BACKGROUND_COLOUR` (stamp), `TEXT_COLOUR`
- Caption templates: `CAPTION_COMING_UP` / `CAPTION_LIVE_NOW` (see `.env.example`)

## Good to know
- GitHub's timer can run 5–15 minutes late at busy times. "Live now" posts are allowed up to 20 minutes after a show starts (`LIVE_GRACE_MINUTES`), so a late run still posts.
- Unscheduled live sets are announced with the DJ's Radio.co collaborator name, or "Live DJ set" if there isn't one.
- Instagram allows 100 API posts per 24 hours, far more than this will use.
