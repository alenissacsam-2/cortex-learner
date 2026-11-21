"""Sources B, part 1 (life_sources): sent mail, notes-to-self chats, a city per day, photo
dates and Spotify years. Every fixture is synthetic and built in tmp_path."""

import re

from typer.testing import CliRunner

from cortex import importers, life_sources, sources
from cortex.cli import app
from cortex.vault import Vault

runner = CliRunner()
C = sources.Audience.CLOUD
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _pages(v: Vault, prefix: str) -> dict[str, str]:
    return {p.stem: p.read_text("utf-8") for p in sorted((v.root / "resources").glob(
        f"{prefix}-*.md"))}


# ---- mail ----

def _mail(frm: str, to: str, date: str, subject: str, body: str, labels: str = "Sent",
          html: bool = False, mid: str = "") -> str:
    ctype = "text/html" if html else "text/plain"
    return (f"From 1234@xxx Mon Mar 02 10:00:00 +0000 2026\nX-Gmail-Labels: {labels}\n"
            f"From: {frm}\nTo: {to}\nDate: {date}\nSubject: {subject}\n"
            f"Message-ID: <{mid or subject.replace(' ', '')}@example.test>\n"
            f"Content-Type: {ctype}; charset=utf-8\n\n{body}\n\n")


MBOX = (
    _mail("Me <me@example.test>", '"Asha Rao" <asha@example.test>, ben@example.test',
          "Tue, 03 Mar 2026 09:30:00 +0000", "Lunch plan",
          "Hi Asha,\nSee you at noon by the library.\nWrite to me at me@example.test.\n\n-- \n"
          "Me\nme@example.test\n\nOn Mon, 2 Mar 2026 at 08:00, Asha <asha@example.test> "
          "wrote:\n> quoted secret plan from Asha\n")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Fri, 10 Apr 2026 18:00:00 +0000", "Notes",
            "<html><body><p>My own html words</p><div class=\"gmail_quote\">On Thu wrote:"
            "<blockquote>chen quoted html text</blockquote></div></body></html>", html=True)
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Sat, 11 Apr 2026 18:00:00 +0000", "Fwd: article",
            "Worth a read.\n\n---------- Forwarded message ---------\nFrom: someone\n"
            "the forwarded article text\n")
    + _mail("Asha <asha@example.test>", "Me <me@example.test>",
            "Tue, 03 Mar 2026 10:30:00 +0000", "Re: Lunch plan", "asha's own reply",
            labels="Inbox")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>", "not a date", "Odd",
            "text")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Sun, 12 Apr 2026 18:00:00 +0000", "Draft one", "unsent words", labels="Draft"))


def test_mail_keeps_own_words_only(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    v = Vault(root_plain)
    src = life_sources.mail_source(box)
    assert src.default_tier is sources.Audience.LOCAL
    r = sources.run(v, src, C)
    assert r["imported"] == 2 and r["reasons"]["not sent by you"] == 1
    assert r["reasons"]["no readable date"] == 1 and r["reasons"]["a draft"] == 1
    pages = _pages(v, "mail")
    assert set(pages) == {"mail-sent-mail-2026-03", "mail-sent-mail-2026-04"}
    march, april = pages["mail-sent-mail-2026-03"], pages["mail-sent-mail-2026-04"]
    assert "See you at noon by the library." in march and "To Asha Rao." in march
    assert "quoted secret plan" not in march and "wrote:" not in march
    assert "\nMe\n" not in march.split("library.")[1]  # the signature is cut
    assert "My own html words" in april and "chen quoted html text" not in april
    assert "Worth a read." in april and "forwarded article text" not in april
    assert "asha's own reply" not in march and "unsent words" not in april
    for text in pages.values():
        assert not _ADDRESS.search(text.split("Source: ")[1].split("\n", 1)[1]), text
    assert "Source: Sent mail · Date: 2026-04-11" in april


def test_mail_reimport_is_a_no_op_and_bad_file_is_counted(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    v = Vault(root_plain)
    sources.run(v, life_sources.mail_source(box), C)
    before = _pages(v, "mail")
    r = sources.run(v, life_sources.mail_source(box), C)
    assert r["imported"] == 0 and r["updated"] == 0 and r["duplicate"] == 2
    assert _pages(v, "mail") == before
    junk = tmp_path / "junk.mbox"
    junk.write_bytes(b"\x00\x01 not mail at all")
    r = sources.run(v, life_sources.mail_source(junk), C)
    assert r["reasons"]["not an mbox file"] == 1 and r["imported"] == 0


def test_an_inbox_top_sender_is_never_taken_as_you(tmp_path, root_plain):
    """Audit 21 Nov I4: who you are comes from Sent mail across all boxes, never from a box's
    most common sender (a newsletter in an Inbox)."""
    news = "".join(_mail("News <news@example.test>", "me@example.test",
                         f"Mon, 0{d} Mar 2026 09:00:00 +0000", f"Issue {d}", f"newsletter {d}",
                         labels="Inbox") for d in range(1, 4))
    (tmp_path / "Inbox.mbox").write_text(news, encoding="utf-8")
    (tmp_path / "Sent.mbox").write_text(MBOX, encoding="utf-8")
    v = Vault(root_plain)
    r = sources.run(v, life_sources.mail_source(tmp_path), C)
    assert r["imported"] == 2 and r["reasons"]["not sent by you"] == 4
    assert "newsletter" not in "".join(_pages(v, "mail").values())
    # an Inbox alone, labelled or not: no owner, nothing imported
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Inbox.mbox").write_text(news.replace("X-Gmail-Labels: Inbox\n", ""),
                                      encoding="utf-8")
    for box in (tmp_path / "Inbox.mbox", plain / "Inbox.mbox"):
        r = sources.run(v, life_sources.mail_source(box), C)
        assert r["imported"] == 0 and r["reasons"]["not sent by you"] == 3, box
    # a Sent folder from a mail app (no Gmail labels) is still yours
    (plain / "Sent.mbox").write_text(news.replace("X-Gmail-Labels: Inbox\n", "")
                                     .replace("news@", "me2@"), encoding="utf-8")
    r = sources.run(v, life_sources.mail_source(plain / "Sent.mbox"), C)
    assert r["imported"] + r["updated"] == 1 and not any(r["reasons"].values()), r


def test_own_text_cuts():
    assert life_sources.own_text("Mine.\n> theirs\nmore mine") == "Mine.\nmore mine"
    assert life_sources.own_text("Mine.\nFrom: Asha\nSent: Monday\ntheirs") == "Mine."
    assert life_sources.own_text("mail x@y.test now") == "mail [address] now"


def test_cli_imports_mail(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    assert importers.detect(box) == ["mail"]
    r = runner.invoke(app, ["import", str(box), "--vault", str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "tier private (the default" in r.output and "Sent mail: read" in r.output
    assert "private: true" in (root_plain / "resources" / "mail-sent-mail-2026-03.md") \
        .read_text("utf-8")


# ---- notes to self ----

WHATSAPP = (
    "13/03/2024, 09:15 - Messages and calls are end-to-end encrypted.\n"
    "13/03/2024, 09:15 - Me: buy milk\nand bread\n"
    "13/03/2024, 21:05 - Me: IMG-20240313-WA0001.jpg (file attached)\n"
    "14/03/2024, 08:00 - Me: <Media omitted>\n"
    "02/04/2024, 7:30 pm - Me: idea: a study timer\n"
    "02/04/2024, 7:31 pm - Me: This message was deleted\n")
IOS = "[03/14/24, 9:15:23 AM] Me: video omitted\n[03/14/24, 9:16:00 AM] Me: note one\n"
TELEGRAM = {"chats": {"list": [
    {"type": "personal_chat", "name": "Asha", "messages": [
        {"type": "message", "date": "2024-03-01T10:00:00", "from_id": "user2",
         "text": "asha's private words"}]},
    {"type": "saved_messages", "messages": [
        {"type": "service", "date": "2024-03-01T10:00:00", "action": "x"},
        {"type": "message", "date": "2024-03-02T10:00:00", "from_id": "user1",
         "text": ["read ", {"type": "bold", "text": "SICP"}, " ch 3"]},
        {"type": "message", "date": "2024-03-03T11:00:00", "from_id": "user1",
         "photo": "photos/p1.jpg", "text": "whiteboard"},
        {"type": "message", "date": "2024-03-04T12:00:00", "from_id": "user1",
         "media_type": "voice_message", "file": "voice/v.ogg", "text": ""},
        {"type": "message", "date": "2024-03-05T12:00:00", "from_id": "user1",
         "forwarded_from": "News", "text": "someone else's post"}]}]}}


def test_selfchat_whatsapp_and_telegram(tmp_path, root_plain):
    import json
    folder = tmp_path / "self"
    folder.mkdir()
    (folder / "WhatsApp Chat with Me.txt").write_text(WHATSAPP, encoding="utf-8")
    (folder / "ios.txt").write_text(IOS, encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(TELEGRAM), encoding="utf-8")
    v = Vault(root_plain)
    src = life_sources.selfchat_source(folder)
    assert src.default_tier is sources.Audience.LOCAL
    r = sources.run(v, src, C)
    assert r["reasons"]["a system line"] == 2 and r["reasons"]["deleted"] == 1
    assert r["reasons"]["forwarded"] == 1
    pages = _pages(v, "selfchat")
    assert set(pages) == {"selfchat-2024-03", "selfchat-2024-04"}
    march, april = pages["selfchat-2024-03"], pages["selfchat-2024-04"]
    assert "- 2024-03-13 09:15 WhatsApp: buy milk / and bread" in march
    assert "- 2024-03-13 21:05 WhatsApp: [photo]" in march
    assert "- 2024-03-14 08:00 WhatsApp: [media]" in march
    assert "- 2024-03-14 09:15 WhatsApp: [video]" in march  # month first, from the 14
    assert "- 2024-04-02 19:30 WhatsApp: idea: a study timer" in april
    assert "- 2024-03-02 10:00 Telegram: read SICP ch 3" in march
    assert "Telegram: [photo] whiteboard" in march and "Telegram: [voice note]" in march
    assert "asha's" not in march and "someone else's post" not in march
    assert "IMG-" not in march and "photos/" not in march
    r = sources.run(v, life_sources.selfchat_source(folder), C)
    assert r["imported"] == 0 and _pages(v, "selfchat") == pages


def test_an_ios_encryption_notice_is_not_a_second_sender(tmp_path, root_plain):
    """Audit 21 Nov I15: iOS puts the end-to-end notice under the chat's name, which need not
    be the name on the owner's own messages; it is a system line, not a second person."""
    f = tmp_path / "WhatsApp Chat - Me (You).txt"
    f.write_text("[03/14/24, 9:15:00 AM] Me (You): ‎Messages and calls are end-to-end "
                 "encrypted. No one outside of this chat, not even WhatsApp, can read or "
                 "listen to them.\n" + IOS, encoding="utf-8")
    r = sources.run(Vault(root_plain), life_sources.selfchat_source(f), C)
    assert r["reasons"]["a system line"] == 1 and r["imported"] == 2, r


def test_selfchat_refuses_a_chat_with_others_and_counts_bad_files(tmp_path, root_plain):
    two = tmp_path / "two"
    two.mkdir()
    (two / "chat.txt").write_text("13/03/2024, 09:15 - Me: hi\n13/03/2024, 09:16 - Asha: "
                                  "her words\n", encoding="utf-8")
    (two / "result.json").write_text("{not json", encoding="utf-8")
    v = Vault(root_plain)
    r = sources.run(v, life_sources.selfchat_source(two), C)
    assert r["reasons"]["not a chat with yourself"] == 1
    assert r["reasons"]["malformed file"] == 1 and r["imported"] == 0
    assert not _pages(v, "selfchat")


def test_cli_imports_selfchat(tmp_path, root_plain):
    f = tmp_path / "WhatsApp Chat with Me.txt"
    f.write_text(WHATSAPP, encoding="utf-8")
    r = runner.invoke(app, ["import", str(f), "--source", "selfchat", "--vault",
                            str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "Notes to self: read" in r.output
    assert "private: true" in (root_plain / "resources" / "selfchat-private-2024-03.md") \
        .read_text("utf-8")


# ---- location ----

def _visit(start: str, address: str | None, lat: int = 129716000) -> dict:
    loc = {"latitudeE7": lat, "longitudeE7": 775946000, "name": "Cafe Secret Spot",
           "placeId": "ChIJabc"}
    if address:
        loc["address"] = address
    return {"placeVisit": {"location": loc, "duration": {"startTimestamp": start}}}


OLD = {"timelineObjects": [
    _visit("2024-03-01T08:00:00Z", "12, 5th Cross, Indiranagar, Bengaluru, Karnataka 560038, "
           "India"),
    _visit("2024-03-01T12:00:00Z", "MG Road, Bengaluru, Karnataka 560001, India"),
    _visit("2024-03-01T18:00:00Z", "Unter den Linden 1, 10117 Berlin, Germany"),
    _visit("2024-03-02T09:00:00Z", None),
    {"activitySegment": {"startLocation": {"latitudeE7": 129716000}, "duration": {
        "startTimestamp": "2024-03-03T09:00:00Z"}}},
    _visit("1711958400000", "1 Infinite Loop, Cupertino, CA 95014, United States")]}
NEW = {"semanticSegments": [
    {"startTime": "2024-04-05T09:00:00.000+05:30", "endTime": "2024-04-05T10:00:00.000+05:30",
     "visit": {"topCandidate": {"placeId": "x", "semanticType": "HOME",
                                "placeLocation": {"latLng": "12.9716123°, 77.5946123°"}}}},
    {"startTime": "2024-04-06T09:00:00.000+05:30",
     "visit": {"topCandidate": {"placeLocation": {"latLng": "48.8566°, 2.3522°",
                                                  "address": "Rue X 5, 75001 Paris, France"}}}},
    {"startTime": "2024-04-06T11:00:00.000+05:30", "timelinePath": [
        {"point": "48.8566°, 2.3522°", "time": "2024-04-06T11:00:00.000+05:30"}]}],
       "rawSignals": [{"position": {"LatLng": "48.8566°, 2.3522°"}}]}


def test_city_from_an_address():
    assert life_sources.city("MG Road, Bengaluru, Karnataka 560001, India") == "Bengaluru"
    assert life_sources.city("Unter den Linden 1, 10117 Berlin, Germany") == "Berlin"
    assert life_sources.city("1 Infinite Loop, Cupertino, CA 95014, USA") == "Cupertino"
    assert life_sources.city("Paris, France") == "Paris"
    assert life_sources.city("10 Downing St, London SW1A 2AA, UK") is None  # never a street
    assert life_sources.city("Main Street 4") is None


def test_location_city_per_day_and_no_coordinates(tmp_path, root_plain):
    import json
    import zipfile
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Takeout/Location History (Timeline)/Semantic Location History/2024/"
                   "2024_MARCH.json", json.dumps(OLD))
        f.writestr("Takeout/Location History (Timeline)/Records.json", '{"locations": []}')
        f.writestr("Takeout/Location History (Timeline)/Semantic Location History/2024/"
                   "2024_BAD.json", "{broken")
    assert importers.detect(z) == ["location"]
    phone = tmp_path / "Timeline.json"
    phone.write_text(json.dumps(NEW), encoding="utf-8")
    v = Vault(root_plain)
    src = life_sources.location_source(z)
    assert src.default_tier is sources.Audience.LOCAL
    r = sources.run(v, src, C)
    assert r["reasons"]["raw points (not read)"] == 1 and r["reasons"]["malformed file"] == 1
    assert r["reasons"]["no place name"] == 1  # 2 March: a visit with no address
    r = sources.run(v, life_sources.location_source(phone), C)
    assert r["reasons"]["no place name"] == 1 and r["imported"] == 1
    pages = _pages(v, "location")
    lines = sorted(x for p in pages.values() for x in p.splitlines() if x.startswith("- "))
    assert lines == ["- 2024-03-01: Bengaluru", "- 2024-04-01: Cupertino",
                     "- 2024-04-06: Paris"]
    for text in pages.values():
        assert not re.search(r"\d+\.\d{2,}", text) and "°" not in text
        assert "Road" not in text and "Cafe" not in text and "Linden" not in text
    r = sources.run(v, life_sources.location_source(z), C)
    assert r["imported"] == 0 and _pages(v, "location") == pages


# ---- photos ----

def _jpeg(path, taken: str | None, gps: bool = True) -> None:
    from PIL import Image
    e = Image.Exif()
    if taken:
        e[0x8769] = {36867: taken}
    if gps:
        e[0x8825] = {1: "N", 2: (12.0, 58.0, 17.0), 3: "E", 4: (77.0, 35.0, 40.0)}
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4)).save(path, "JPEG", exif=e)


def test_photos_counts_per_day_with_albums_and_no_gps(tmp_path, root_plain):
    import os
    pics = tmp_path / "Pictures"
    _jpeg(pics / "Goa trip" / "a.jpg", "2024:03:12 10:00:00")
    _jpeg(pics / "Goa trip" / "b.JPG", "2024:03:12 11:00:00")
    _jpeg(pics / "Camera" / "c.jpg", "2024:03:12 12:00:00")
    _jpeg(pics / "d.jpg", None)  # no capture date: the file's date
    (pics / "Camera" / "broken.jpg").write_bytes(b"not an image")
    (pics / "Camera" / "clip.mp4").write_bytes(b"video")
    (pics / "Camera" / "c.jpg.json").write_text('{"geoData": {"latitude": 12.97}}')
    _jpeg(pics / ".thumbs" / "t.jpg", "2024:03:12 10:00:00")
    stamp = 1714557600  # 2024-05-01 10:00 UTC; the page uses the local day
    for f in ("d.jpg", "Camera/broken.jpg"):
        os.utime(pics / f, (stamp, stamp))
    v = Vault(root_plain)
    src = life_sources.photos_source(pics)
    assert src.default_tier is sources.Audience.CLOUD
    r = sources.run(v, src, C)
    assert r["reasons"]["not a photo"] == 2 and r["imported"] == 2
    pages = _pages(v, "photos")
    assert "- 2024-03-12: 3 photos (Camera, Goa trip)" in pages["photos-2024-03"]
    may = pages["photos-2024-05"]
    assert re.search(r"- 2024-0[45]-\d\d: 2 photos \(Camera, Pictures\)", may)
    for text in pages.values():
        assert "12.97" not in text and "58" not in text and "77" not in text
        assert ".jpg" not in text and "thumbs" not in text
    r = sources.run(v, life_sources.photos_source(pics), C)
    assert r["imported"] == 0 and _pages(v, "photos") == pages


def test_cli_imports_photos(tmp_path, root_plain):
    _jpeg(tmp_path / "Pictures" / "Camera" / "a.jpg", "2024:03:12 10:00:00")
    r = runner.invoke(app, ["import", str(tmp_path / "Pictures"), "--source", "photos",
                            "--vault", str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "Photos: tier normal (the default" in r.output
    assert (root_plain / "resources" / "photos-2024-03.md").is_file()


# ---- Spotify ----

H = 3_600_000  # an hour in ms
ACCOUNT = [{"endTime": "2023-12-31 23:00", "artistName": "Old Band", "trackName": "Old Song",
            "msPlayed": 2 * H},
           {"endTime": "2024-01-02 10:00", "artistName": "Band A", "trackName": "Song 1",
            "msPlayed": H}]
PODCASTS = [{"endTime": "2024-02-01 08:00", "podcastName": "Lex Talks", "episodeName": "Ep 1",
             "msPlayed": H // 2}]
EXTENDED = [
    {"ts": "2024-01-02T10:00:00Z", "ms_played": 3 * H, "conn_country": "IN",
     "ip_addr": "203.0.113.7", "platform": "android", "master_metadata_track_name": "Song 1",
     "master_metadata_album_artist_name": "Band A", "episode_show_name": None},
    {"ts": "2024-03-05T10:00:00Z", "ms_played": H, "ip_addr": "203.0.113.7",
     "master_metadata_track_name": "Song 2", "master_metadata_album_artist_name": "Band B"},
    {"ts": "2024-03-06T10:00:00Z", "ms_played": H // 2, "master_metadata_track_name": None,
     "episode_show_name": "Lex Talks", "episode_name": "Ep 2"},
    {"ts": "2024-03-07T10:00:00Z", "ms_played": 0, "master_metadata_track_name": "Skipped",
     "master_metadata_album_artist_name": "Band C"}]


def test_spotify_year_pages_from_either_shape(tmp_path, root_plain):
    import json
    import zipfile
    acct = tmp_path / "account.zip"
    with zipfile.ZipFile(acct, "w") as z:
        z.writestr("Spotify Account Data/StreamingHistory_music_0.json", json.dumps(ACCOUNT))
        z.writestr("Spotify Account Data/StreamingHistory_podcast_0.json",
                   json.dumps(PODCASTS))
        z.writestr("Spotify Account Data/StreamingHistory_music_1.json", "[broken")
        z.writestr("Spotify Account Data/Userdata.json", '{"email": "me@example.test"}')
    assert importers.detect(acct) == ["spotify"]
    v = Vault(root_plain)
    r = sources.run(v, life_sources.spotify_source(acct), C)
    assert r["imported"] == 2 and r["reasons"]["malformed file"] == 1
    pages = _pages(v, "listening")
    y24 = pages["listening-spotify-2024"]
    assert "My Spotify listening in 2024: 1.5 hours (music 1.0, podcasts 0.5)." in y24
    assert "1. Band A: 1.0 hours" in y24 and '1. "Song 1" by Band A: 1.0 hours' in y24
    assert "1. Lex Talks: 0.5 hours" in y24 and "Old Band" not in y24
    assert "Old Band" in pages["listening-spotify-2023"]
    assert "@" not in "".join(pages.values())
    r = sources.run(v, life_sources.spotify_source(acct), C)
    assert r["imported"] == 0 and r["updated"] == 0 and _pages(v, "listening") == pages

    ext = tmp_path / "extended"
    ext.mkdir()
    (ext / "Streaming_History_Audio_2024.json").write_text(json.dumps(EXTENDED), "utf-8")
    (ext / "StreamingHistory_music_0.json").write_text(json.dumps(ACCOUNT), "utf-8")
    r = sources.run(v, life_sources.spotify_source(ext), C)
    assert r["reasons"]["covered by the extended history"] == 1 and r["updated"] == 1
    y24 = _pages(v, "listening")["listening-spotify-2024"]
    assert "4.5 hours (music 4.0, podcasts 0.5)" in y24 and "Skipped" not in y24
    assert '1. "Song 1" by Band A: 3.0 hours\n2. "Song 2" by Band B: 1.0 hours' in y24
    assert "203.0.113.7" not in y24 and "android" not in y24 and "IN" not in y24.split()
    assert "2024-01-02" not in y24.split("\n\n", 2)[2]  # nothing per play


def test_a_partial_export_never_shrinks_a_year_or_month(tmp_path, root_plain):
    """Audit 21 Nov I6: a slice of a Spotify history or of a Sent folder, imported after the
    whole one, leaves the bigger year or month page as it is."""
    import json
    v = Vault(root_plain)
    full, part = tmp_path / "full", tmp_path / "part"
    for d, rows in ((full, EXTENDED), (part, EXTENDED[1:2])):
        d.mkdir()
        (d / "Streaming_History_Audio_2024.json").write_text(json.dumps(rows), "utf-8")
    sources.run(v, life_sources.spotify_source(full), C)
    before = _pages(v, "listening")
    r = sources.run(v, life_sources.spotify_source(part), C)
    assert r["updated"] == 0 and _pages(v, "listening") == before
    (full / "Sent.mbox").write_text(MBOX, encoding="utf-8")
    april = "From 1234@xxx" + MBOX.split("From 1234@xxx")[2]  # one of April's two messages
    (part / "Sent.mbox").write_text(april, encoding="utf-8")
    sources.run(v, life_sources.mail_source(full / "Sent.mbox"), C)
    before = _pages(v, "mail")
    assert "2 messages" in before["mail-sent-mail-2026-04"]
    sources.run(v, life_sources.mail_source(part / "Sent.mbox"), C)
    assert _pages(v, "mail") == before
