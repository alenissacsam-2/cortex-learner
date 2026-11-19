"""Built-in lists for the browse privacy filter (docs/design/browse-brain.md §2). Modest on
purpose: a domain here matches itself and its subdomains; the user's own lists live under
`browse:` in ~/.cortex/config.yaml. Nothing is downloaded."""

from __future__ import annotations

# §2.1 hard drop: sign-in and account hosts; AUTH_WORDS match any word of a decoded path
# segment or host label ("/wp-login.php", "/%6Cogin", "/customer_login")
AUTH_HOSTS = frozenset({
    "accounts.google.com", "myaccount.google.com", "appleid.apple.com",
    "account.microsoft.com", "account.live.com", "login.live.com",
    "login.microsoftonline.com", "id.atlassian.com", "auth0.com", "okta.com",
})
AUTH_WORDS = frozenset({
    "login", "logon", "logout", "signin", "signon", "signup", "oauth", "oauth2", "auth",
    "authorize", "authenticate", "authentication", "authn", "sso", "saml", "password",
    "passwords", "passwd", "2fa", "mfa", "otp", "verify", "verification", "recover",
    "sessions",
})

# §2.1 payment, checkout and bank pages: PAY_WORDS match a part's first word (or first two
# joined), BANK_WORDS any word; a host label containing BANK_LABEL drops too, and so does a title
# holding one of BANK_TITLES
PAY_HOSTS = frozenset({
    "paypal.com", "checkout.stripe.com", "billing.stripe.com", "razorpay.com", "payu.in",
    "payu.com", "ccavenue.com", "billdesk.com", "phonepe.com", "paytm.com", "pay.google.com",
    "payments.google.com", "pay.amazon.com", "pay.amazon.in", "wise.com", "revolut.com",
})
PAY_WORDS = frozenset({"checkout", "payment", "payments", "pay", "paynow", "billing",
                       "purchase"})
BANK_LABEL = "bank"
BANK_WORDS = frozenset({"bank", "banking", "netbanking", "ebanking", "ibanking"})
BANK_TITLES = ("net banking", "netbanking", "internet banking", "online banking",
               "mobile banking", "account summary", "account statement", "bank statement",
               "card statement", "e-statement")
BANK_HOSTS = frozenset({
    "onlinesbi.sbi", "sbi.co.in", "hdfcbank.com", "icicibank.com", "axisbank.com",
    "kotak.com", "pnbindia.in", "netpnb.com", "bankofbaroda.in", "bobibanking.com",
    "canarabank.com", "unionbankofindia.co.in", "idfcfirstbank.com", "yesbank.in",
    "indusind.com", "federalbank.co.in", "aubank.in", "rblbank.com", "chase.com",
    "bankofamerica.com", "wellsfargo.com", "citi.com", "citibank.com", "hsbc.com",
    "hsbc.co.in", "barclays.co.uk", "capitalone.com", "americanexpress.com",
})

# §2.1 webmail: kept as the site name only, never the subject
WEBMAIL_HOSTS = frozenset({
    "mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com",
    "mail.yahoo.com", "mail.proton.me", "mail.zoho.com", "mail.zoho.in", "app.fastmail.com",
    "mail.aol.com", "mail.yandex.com",
})

# §2.1 query keys that carry secrets (a key containing one of SECRET_PARTS counts too)
SECRET_KEYS = frozenset({"token", "code", "key", "session", "sig", "auth", "password",
                         "secret", "apikey", "access_token"})
SECRET_PARTS = ("token", "secret", "password", "passwd", "session", "apikey", "api_key",
                "signature")

# §2.4 sensitive categories: domains (sure) and words (title, search, channel or URL words,
# casefolded, any script). A word in STRONG or a host label starting with an ADULT_BRANDS name
# is sure; any other word hit is a "maybe", which is private just the same. Since the second
# review a hit only keeps an item's time off the public page (no title is ever public), so the
# lists lean wide; "std::" (C++) is still not a health word.
CATEGORY_HOSTS = {
    "adult": frozenset({
        "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com", "redtube.com",
        "youporn.com", "onlyfans.com", "fansly.com", "chaturbate.com", "stripchat.com",
        "livejasmin.com", "spankbang.com", "eporner.com", "rule34.xxx", "e-hentai.org",
        "nhentai.net", "brazzers.com", "motherless.com", "tnaflix.com", "beeg.com",
        "porn.com", "thumbzilla.com",
    }),
    "health": frozenset({
        "webmd.com", "mayoclinic.org", "healthline.com", "practo.com", "1mg.com",
        "pharmeasy.in", "netmeds.com", "medlineplus.gov", "nhs.uk", "drugs.com",
        "apollo247.com", "clevelandclinic.org", "psychologytoday.com", "betterhelp.com",
        "talkspace.com", "7cups.com", "patient.info",
    }),
    "money": frozenset({
        "zerodha.com", "groww.in", "upstox.com", "angelone.in", "icicidirect.com",
        "hdfcsec.com", "5paisa.com", "robinhood.com", "etrade.com", "schwab.com",
        "fidelity.com", "vanguard.com", "coinbase.com", "binance.com", "wazirx.com",
        "coindcx.com", "kraken.com", "cred.club", "cibil.com", "creditkarma.com",
    }),
    "betting": frozenset({
        "bet365.com", "betway.com", "dream11.com", "my11circle.com", "mpl.live",
        "rummycircle.com", "jungleerummy.com", "pokerstars.com", "stake.com", "1xbet.com",
        "parimatch.com", "fanduel.com", "draftkings.com", "betfair.com", "williamhill.com",
    }),
    "dating": frozenset({
        "tinder.com", "bumble.com", "hinge.co", "okcupid.com", "match.com", "aisle.co",
        "trulymadly.com", "happn.com", "grindr.com", "badoo.com", "pof.com",
        "eharmony.com", "shaadi.com", "bharatmatrimony.com", "jeevansathi.com",
        "coffeemeetsbagel.com", "feeld.co",
    }),
    "sexuality": frozenset(),
    "drugs": frozenset({"leafly.com", "erowid.org"}),
    "selfharm": frozenset(),
}
ADULT_TLDS = ("xxx", "porn", "sex", "adult", "sexy")
# Words match a normalised word exactly (privacy._norm: NFKC, casefold, no zero-width or soft
# hyphen, Cyrillic/Greek look-alikes and leetspeak mapped, "p o r n" joined); STEMS match a
# word's start ("masturbat" -> masturbation); PHRASES match whole words in a row.
CATEGORY_WORDS = {
    "adult": frozenset({"porn", "porno", "pornhub", "xxx", "nsfw", "hentai", "onlyfans",
                        "blowjob", "milf", "camgirl", "camgirls", "rule34", "sex", "sexy",
                        "nude", "nudes", "erotic", "erotica", "fetish", "lingerie", "naked",
                        "vibrator", "dildo", "boobs", "bikini", "escort", "escorts",
                        "stepmom", "bhabhi", "nofap", "pron", "chudai", "mms", "सेक्स", "हस्तमैथुन"}),
    "health": frozenset({"symptoms", "symptom", "diagnosis", "hiv", "abortion",
                         "prescription", "dosage", "psychiatrist", "therapist", "therapy",
                         "antidepressant", "antidepressants", "herpes", "depression",
                         "depressed", "medication", "medications", "std", "stds", "sti", "stis",
                         "chlamydia", "gonorrhea", "gonorrhoea", "syphilis", "hpv", "condom",
                         "condoms", "anxiety", "anxious", "adhd", "ocd", "bipolar",
                         "schizophrenia", "ptsd", "cancer", "tumor", "tumour", "biopsy",
                         "rehab", "overdose", "erectile", "dysmorphia", "bulimia", "anorexia",
                         "dyslexia", "adderall", "accutane", "propranolol", "ozempic",
                         "steroid", "steroids", "testosterone", "gynecomastia", "insomnia",
                         "concussion", "disability",
                         # C1 (v7 review): wider, but never a plain study word
                         "pcos", "pcod", "endometriosis", "miscarriage", "infertility", "ivf",
                         "vaginal", "vagina", "penis", "impotence", "hemorrhoids", "diabetes",
                         "diabetic", "epilepsy", "autism", "autistic", "dementia", "alzheimers",
                         "chemotherapy", "chemo", "eczema", "psoriasis", "uti", "psychosis",
                         "psychotic", "addiction", "addicted", "sobriety", "dysphoria",
                         "thyroid", "hypothyroidism", "migraine", "acne", "hairfall"}),
    "money": frozenset({"demat", "netbanking", "cibil", "bankruptcy", "bankrupt", "payday",
                        "foreclosure", "overdraft", "repossession", "emis"}),
    "betting": frozenset({"betting", "casino", "rummy", "jackpot", "sportsbook", "patti",
                          "gambling", "satta", "matka", "1xbet"}),
    "dating": frozenset({"dating", "matrimony", "matrimonial", "hookup", "tinder", "grindr",
                         "sugardaddy"}),
    "sexuality": frozenset({"gay", "lesbian", "bisexual", "queer", "transgender", "lgbt",
                            "lgbtq", "lgbtqia", "sexuality", "homosexual", "asexual", "pansexual",
                            "nonbinary", "intersex", "genderqueer", "genderfluid", "aromantic",
                            "demisexual", "polyamory", "polyamorous"}),
    "drugs": frozenset({"weed", "cocaine", "lsd", "mdma", "marijuana", "cannabis", "ganja",
                        "charas", "heroin", "meth", "ketamine", "shrooms", "psilocybin",
                        "opium", "vape", "vaping", "stoned", "xanax", "valium", "codeine",
                        "tramadol", "oxycodone", "fentanyl", "percocet", "benzos", "dmt",
                        "ayahuasca", "kratom", "edibles", "thc"}),
    "selfharm": frozenset({"selfharm", "self-harm", "suicide", "suicidal", "unalive",
                           "self-injury"}),
}
STEMS = {
    "adult": ("porn", "sex", "masturbat", "nofap", "onlyfans", "xvideo", "xhamster"),
    "health": ("pregnan", "contracepti", "gonorrh", "antidepress", "psychiatr", "schizo",
               "gynec", "gynaec", "obstetric", "oncolog", "infertil", "endometrio", "miscarr",
               "chemotherap", "dementi", "alzheimer"),
    "betting": ("gambl",),
    "selfharm": ("suicid",),
}
PHRASES = {
    "adult": ("only fans",),
    "money": ("credit score", "loan default", "salary slip", "loan emi", "emi calculator",
              "personal loan", "home loan", "education loan", "car loan", "gold loan",
              "loan app", "instant loan", "payday loan", "debt collector", "debt relief",
              "debt consolidation", "in debt", "credit card debt", "credit card bill",
              "loan recovery", "cheque bounce"),
    "betting": ("satta matka", "teen patti"),
    "dating": ("sugar daddy", "dating app"),
    "sexuality": ("gender identity", "am i trans", "gender dysphoria"),
    "drugs": ("acid trip", "magic mushrooms", "drug test", "pass a drug test"),
    "selfharm": ("self harm", "kill myself", "killing myself", "end my life", "want to die",
                 "how to die", "cut myself", "cutting myself", "hurt myself",
                 # C1: the review's "ways to stop existing" and its kind
                 "stop existing", "don t want to live", "dont want to live",
                 "do not want to live", "no reason to live", "better off dead", "end it all",
                 "painless death", "wish i was dead", "wish i were dead", "wanna die",
                 "slit my wrists", "hang myself", "self injury", "not want to be alive",
                 "dont want to be alive", "don t want to be alive"),
    "health": ("morning after pill", "panic attack", "panic attacks", "mental illness",
               "sexual health", "period late", "periods late", "missed period",
               "eating disorder", "binge eating", "birth control", "yeast infection",
               "period pain", "mental health", "hair loss", "weight loss pills",
               "parkinson s disease", "parkinsons disease"),
}
# "sexuality" before the adult stem "sex": an exact word anywhere wins over a stem
STEM_ORDER = ("selfharm", "health", "betting", "adult")
STRONG = frozenset({"porn", "porno", "pornhub", "xxx", "nsfw", "hentai", "onlyfans",
                    "blowjob", "milf", "camgirl", "camgirls", "rule34", "xvideos",
                    "xhamster", "xnxx", "redtube", "youporn", "spankbang", "brazzers",
                    "chaturbate", "stripchat", "livejasmin", "eporner", "fansly", "nhentai"})
ADULT_BRANDS = ("porn", "xvideos", "xhamster", "xnxx", "redtube", "youporn", "onlyfans",
                "fansly", "chaturbate", "stripchat", "livejasmin", "spankbang", "eporner",
                "brazzers", "nhentai", "tnaflix", "thumbzilla")
CATEGORIES = tuple(CATEGORY_HOSTS)  # adult first: it wins a tie
# the categories of v0.1.0, when configs stored `hidden_from_claude`: any category added since
# is local for such a config (C9: the cloud allowlist `cloud_categories` replaced it)
CATEGORIES_V1 = ("adult", "health", "money", "betting", "dating", "sexuality", "drugs",
                 "selfharm")
STRONG_STEMS = ("porn", "onlyfans", "xvideo", "xhamster")  # a word starting so is a sure adult hit
# Normalisation for matching only: look-alike letters and common leetspeak
CONFUSABLES = str.maketrans({
    **dict(zip("аеорсухіјѕԁɡһӏ", "aeopcyxijsdghl", strict=True)),  # Cyrillic and Latin look-alikes
    **dict(zip("αεορυχικντ", "aeopyxikvt", strict=True)),  # Greek
    **dict.fromkeys("\u200b\u200c\u200d\u2060\ufeff\u00ad"),  # zero-width, soft hyphen: dropped
})
LEET = str.maketrans("013457@$", "oieastas")

# §3a site pages: a non-sensitive site taxonomy. A sensitive category (`privacy.category` on the
# host) always wins over it; the local LLM may only name one of these or a sensitive category.
SITE_CATEGORIES = ("programming", "ai", "learning", "news", "video", "social", "shopping",
                   "work", "reference", "gaming", "music", "travel", "food", "finance-news",
                   "other")
_SITES = {
    "programming": "github.com gitlab.com bitbucket.org stackoverflow.com stackexchange.com "
                   "python.org pypi.org npmjs.com developer.mozilla.org docs.rs crates.io "
                   "rust-lang.org go.dev golang.org nodejs.org reactjs.org react.dev vuejs.org "
                   "w3schools.com geeksforgeeks.org leetcode.com hackerrank.com codeforces.com "
                   "codechef.com kaggle.com docker.com kubernetes.io readthedocs.io "
                   "readthedocs.org learn.microsoft.com code.visualstudio.com jetbrains.com "
                   "vercel.com netlify.com heroku.com render.com dev.to hashnode.com "
                   "freecodecamp.org replit.com codepen.io jsfiddle.net sqlite.org "
                   "postgresql.org mysql.com mongodb.com cppreference.com cplusplus.com "
                   "oracle.com java.com typescriptlang.org tailwindcss.com getbootstrap.com "
                   "astral.sh pytorch.org tensorflow.org numpy.org pandas.pydata.org",
    "ai": "openai.com chatgpt.com chat.openai.com claude.ai anthropic.com gemini.google.com "
          "perplexity.ai huggingface.co mistral.ai deepseek.com ollama.com lmstudio.ai "
          "poe.com character.ai midjourney.com stability.ai copilot.microsoft.com "
          "aistudio.google.com notebooklm.google.com grok.com x.ai cohere.com together.ai "
          "groq.com openrouter.ai",
    "learning": "coursera.org udemy.com edx.org khanacademy.org brilliant.org nptel.ac.in "
                "swayam.gov.in byjus.com unacademy.com duolingo.com mit.edu ocw.mit.edu "
                "stanford.edu harvard.edu arxiv.org scholar.google.com researchgate.net "
                "sciencedirect.com springer.com ieee.org acm.org jstor.org quizlet.com "
                "chegg.com physicswallah.com udacity.com skillshare.com codecademy.com "
                "datacamp.com classroom.google.com",
    "news": "bbc.com bbc.co.uk cnn.com nytimes.com theguardian.com reuters.com apnews.com "
            "thehindu.com indianexpress.com hindustantimes.com timesofindia.indiatimes.com "
            "ndtv.com news.google.com aljazeera.com washingtonpost.com theverge.com "
            "techcrunch.com wired.com arstechnica.com engadget.com news.ycombinator.com "
            "livemint.com theprint.in scroll.in thewire.in firstpost.com indiatoday.in "
            "news18.com economist.com ft.com bloomberg.com forbes.com",
    "video": "youtube.com youtu.be netflix.com primevideo.com hotstar.com jiocinema.com "
             "vimeo.com twitch.tv dailymotion.com sonyliv.com zee5.com crunchyroll.com "
             "disneyplus.com hulu.com max.com mxplayer.in miruro.tv hianime.to aniwatch.to "
             "9anime.to gogoanime.to animepahe.ru animekai.to",
    "social": "reddit.com twitter.com x.com facebook.com instagram.com linkedin.com "
              "threads.net bsky.app mastodon.social discord.com whatsapp.com web.whatsapp.com "
              "telegram.org web.telegram.org snapchat.com pinterest.com tumblr.com quora.com "
              "tiktok.com",
    "shopping": "amazon.com amazon.in flipkart.com myntra.com ajio.com meesho.com "
                "nykaa.com ebay.com etsy.com aliexpress.com walmart.com bestbuy.com "
                "croma.com reliancedigital.in snapdeal.com tatacliq.com ikea.com "
                "decathlon.in decathlon.com shopify.com",
    "work": "docs.google.com drive.google.com sheets.google.com slides.google.com "
            "calendar.google.com meet.google.com notion.so notion.site slack.com zoom.us "
            "teams.microsoft.com office.com microsoft365.com onedrive.live.com "
            "sharepoint.com dropbox.com trello.com asana.com atlassian.net atlassian.com "
            "jira.com figma.com canva.com miro.com airtable.com clickup.com monday.com "
            "linear.app overleaf.com",
    "reference": "wikipedia.org wikimedia.org wiktionary.org wikihow.com britannica.com "
                 "merriam-webster.com dictionary.com thesaurus.com archive.org "
                 "maps.google.com translate.google.com wolframalpha.com desmos.com "
                 "imdb.com goodreads.com fandom.com timeanddate.com weather.com "
                 "accuweather.com google.com bing.com duckduckgo.com search.brave.com",
    "gaming": "store.steampowered.com steampowered.com steamcommunity.com epicgames.com "
              "chess.com lichess.org roblox.com minecraft.net ign.com gamespot.com "
              "nexusmods.com itch.io xbox.com playstation.com nintendo.com riotgames.com "
              "op.gg",
    "music": "spotify.com open.spotify.com music.youtube.com soundcloud.com "
             "music.apple.com gaana.com jiosaavn.com wynk.in bandcamp.com genius.com "
             "last.fm",
    "travel": "booking.com airbnb.com makemytrip.com goibibo.com irctc.co.in "
              "tripadvisor.com expedia.com skyscanner.com cleartrip.com ixigo.com "
              "redbus.in uber.com olacabs.com agoda.com",
    "food": "zomato.com swiggy.com allrecipes.com bbcgoodfood.com seriouseats.com "
            "food.com tasty.co hebbarskitchen.com archanaskitchen.com blinkit.com "
            "zeptonow.com bigbasket.com dominos.co.in",
    "finance-news": "moneycontrol.com economictimes.indiatimes.com investing.com "
                    "tradingview.com finance.yahoo.com marketwatch.com cnbc.com "
                    "screener.in tickertape.in valueresearchonline.com investopedia.com",
}
SITE_HOSTS = {d: c for c, ds in _SITES.items() for d in ds.split()}
# hosts where each subdomain is someone's own site ("ana.github.io"); medium.com/@user and
# substack-style paths are handled in build/sites.site_of
MULTI_TENANT = ("github.io", "gitlab.io", "blogspot.com", "substack.com", "medium.com",
                "vercel.app", "netlify.app", "pages.dev", "notion.site", "wordpress.com",
                "tumblr.com", "herokuapp.com", "web.app", "firebaseapp.com", "neocities.org",
                "readthedocs.io", "hashnode.dev", "streamlit.app", "glitch.me", "carrd.co",
                "wixsite.com", "weebly.com", "bearblog.dev", "framer.website", "onrender.com",
                "fly.dev", "replit.app", "itch.io")
# second-level labels under a country code ("bbc.co.uk" is a site, "co.uk" is not)
SECOND_LEVEL = frozenset({"co", "com", "ac", "gov", "org", "net", "edu", "nic", "res", "gen",
                          "ind", "firm", "mil", "ne", "or", "go"})
