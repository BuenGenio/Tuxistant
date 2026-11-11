# Where the "Works with" marks came from

The marks in the ticker below the hero, one file each. `build.py` draws them inline from these
files, in the text colour, at the height set in `MARKS`. The list of brands and their Now or Soon
status is `WORKS_WITH` in `build.py`.

Every file was made the same way: the path data was copied character for character from the source
named below, the colour attributes were dropped so the mark follows the text colour, and the
`viewBox` was cropped to the drawing. Nothing was redrawn, traced or reshaped.

Fetched on 2026-11-11. "Simple Icons" means the npm package `simple-icons` at version 16.37.0
(https://github.com/simple-icons/simple-icons), whose collection is released under CC0 1.0. Its
disclaimer says that does not make every brand's artwork CC0, and that trademarks stay with their
owners, so the brand's own terms are listed too. "Commons" means Wikimedia Commons, with the licence
tag on the file's page that day.

| File | Brand | Source | Copyright terms | Trademark of |
|---|---|---|---|---|
| `linux.svg` | Linux (Tux) | Simple Icons `icons/linux.svg` | CC0 path data. Tux was drawn by Larry Ewing with The GIMP, who allows use and changes if he is credited. The site footer credits him | Linux: Linus Torvalds |
| `gnome.svg` | GNOME | Simple Icons `icons/gnome.svg` | CC0 path data. The GNOME foot itself is LGPL (Commons, `File:Gnomelogo.svg`) | The GNOME Foundation. Rules: https://foundation.gnome.org/logo-and-trademarks/ |
| `bbc.svg` | BBC | Commons, `File:BBC_Logo_2021.svg` | Public domain as plain lettering and shapes (`PD-textlogo`) | The BBC |
| `kyiv-independent.svg` | The Kyiv Independent | Commons, `File:The_Kyiv_Independent_logo.svg` (the stacked 2022 logo) | Public domain under Ukrainian law, which does not protect organisations' emblems (`PD-UA-exempt`) | The Kyiv Independent |
| `google-calendar.svg` | Google Calendar | Simple Icons `icons/googlecalendar.svg`, its one-colour form of Google's 2020 product icon | CC0 path data. Commons tags the icon public domain (`PD-logo`) | Google |
| `tapo.svg` | Tapo | The logo in the header of Tapo's own site: https://static.tapo.com/res/new-home/tapo.svg, as used on https://www.tapo.com/en | No licence is published for it. See "Tapo" below | TP-Link |
| `libretranslate.svg` | LibreTranslate | Its own repository: `libretranslate/static/icon.svg`, https://github.com/LibreTranslate/LibreTranslate/blob/a23a9fbd7528d7f37dc2b88114f119e339cadc4d/libretranslate/static/icon.svg. The file keeps the source's three `transform`s as written | The repository is AGPL-3.0. Its `TRADEMARK.md` governs the logo. See "LibreTranslate" below | LibreTranslate (contact: UAV4GEO) |
| `home-assistant.svg` | Home Assistant | Simple Icons `icons/homeassistant.svg`, which took it from the logo archive in https://github.com/home-assistant/assets | CC BY-NC-SA 4.0. See "Home Assistant" below | Home Assistant, a project of the Open Home Foundation |
| `spotify.svg` | Spotify | Simple Icons `icons/spotify.svg` | CC0 path data. Commons tags the icon `PD-textlogo` | Spotify. Rules: https://developer.spotify.com/documentation/design |
| `intelimaris.svg` | InteliMaris | The company's own design package: its round badge, light artwork | The owner's own mark | InteliMaris (the owner's company) |
| `cnn.svg` | CNN | Simple Icons `icons/cnn.svg` | CC0 path data. Commons tags the logo `PD-textlogo` | Cable News Network |
| `windows.svg` | Windows | Commons, `File:Windows_logo_-_2021.svg` | Public domain as plain shapes (`PD-textlogo`) | Microsoft |
| `macos.svg` | macOS | Simple Icons `icons/macos.svg`, taken from Commons `File:MacOS_wordmark_(2017).svg` | CC0 path data. Commons tags the wordmark `PD-textlogo` | Apple |

## Set in text, with no mark

| Brand | Why |
|---|---|
| Open-Meteo | It publishes no logo. Its site sets the name beside a stock weather icon (Bootstrap Icons `cloud-sun`), and its repositories hold only small raster favicons |

## Home Assistant

The Home Assistant logo is by Home Assistant (https://github.com/home-assistant/assets) and is
licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(https://creativecommons.org/licenses/by-nc-sa/4.0/). Changes made here: drawn in one colour and
cropped to the drawing, from Simple Icons' one-path form of it. `home-assistant.svg` is shared under
the same licence.

What that licence asks for:

- **Attribution** wherever the logo is shown. This file gives it; the site's pages do not.
- **NonCommercial**: not for use "primarily intended for or directed towards commercial advantage
  or monetary compensation".
- **ShareAlike**: a changed copy carries the same licence.

"Works with Home Assistant" is also the name of Home Assistant's certification programme for
devices (https://works-with.home-assistant.io/), which has its own badge. Tuxistant is not in that
programme. The ticker shows the plain logo under the site's own "Works with" label and must never
show that badge. Logo rules: https://design.home-assistant.io/#brand/logo

## LibreTranslate

The repository's licence (AGPL-3.0) covers the file. For the logo as a trademark its README points
to https://github.com/LibreTranslate/LibreTranslate/blob/main/TRADEMARK.md, which says the software
licence "does not give permission to use LibreTranslate's trademarks", and:

- Without specific permission, the logo may be used in unmodified LibreTranslate software, and "in
  visuals to truthfully refer to and/or to link to the applicable programs, products, services and
  technologies hosted on LibreTranslate servers".
- A project may "state in words (not using logos or images)" that it "works with" or "is compatible"
  with LibreTranslate software, if that is true.
- "Don't modify LibreTranslate's trademarks", and "All other uses of an LibreTranslate trademark
  require our prior written permission", asked for at https://uav4geo.com/contact.

The ticker shows the logo, in one colour, to say Tuxistant works with LibreTranslate. That is
outside what the file allows without written permission, and none has been asked for. In the
repository the logo is drawn in white for the app's coloured header; here it follows the text colour.

## Tapo

TP-Link publishes no brand kit or licence for the Tapo logo that could be found. Its website terms
(https://privacy.tp-link.com/web/official/terms-of-use) say: "You may not use the content of the
Services in any other public or commercial way nor may you copy or incorporate any of the content of
the Services into any other work, including your own website without the prior written consent of
TP-Link." No consent has been asked for. The mark is Tapo's own wordmark; TP-Link's corporate logo is
not used in its place.

## The other owners' rules

Being free of copyright does not settle trademark use. None of these owners has been asked, and
several say in their own rules that a logo needs their permission while the name in plain text
does not:

- **BBC** and **Windows**: Simple Icons removed both for that reason, BBC in version 10
  (https://github.com/simple-icons/simple-icons/pull/9870) and Microsoft's marks in version 13
  (https://github.com/simple-icons/simple-icons/pull/10019). That is why these two come from Commons.
- **macOS**: Apple's rules allow its word marks in a phrase such as "compatible with", only for a
  product that already is, and ask for a licence for its logos
  (https://www.apple.com/legal/intellectual-property/guidelinesfor3rdparties.html).
- **Google Calendar**: Google lists product icons under "ask first" and its names in plain text
  under "go for it" (https://about.google/brand-resource-center/guidance/). The one-colour icon is
  also Simple Icons' simplification of Google's four-colour one.
- **Spotify**: its design rules are written for apps that use Spotify's content, which the planned
  media control (any MPRIS player) is not. They do allow a one-colour logo, black on light and white
  on dark.
- **CNN** and **The Kyiv Independent** publish no rules that allow this use.

To show a brand as its name instead, change its mark to `None` in `WORKS_WITH`. The footer's
trademark line names every brand shown and says none of them sponsors or endorses Tuxistant;
`tests/test_website.py` fails if a name is missing from it.
