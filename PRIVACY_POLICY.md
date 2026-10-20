# Privacy Policy

_Last updated: October 20, 2026_

This Privacy Policy explains what information the Nightscout Discord Bot
("the Bot", "we", "us") collects, how it is used, and the choices you have.
By using the Bot you agree to this policy. If you do not agree, do not use
the Bot.

The Bot is an independent, open-source project. It is not affiliated with,
endorsed by, or sponsored by Discord Inc., the Nightscout Foundation, or
Nocturne.

## Summary

- The Bot stores **only the settings you give it**, keyed to your Discord
  user ID: your Nightscout site URL, your credentials, and display
  preferences.
- The Bot **does not store your glucose, insulin, or device data**. It reads
  that data from your Nightscout site when you run a command, builds the
  reply, and discards it.
- Some commands post your data **publicly in the channel** where you run
  them. See [Who can see your data](#who-can-see-your-data).
- You can delete everything the Bot stores about you at any time with
  `/ns clear`.

## Information we collect

### Information you provide

When you configure the Bot with `/ns setup` and the other `/ns` (or `/bg`)
commands, we store:

| Data | Purpose |
| --- | --- |
| Your Discord user ID | To associate your settings with you |
| Nightscout / Nocturne site URL | To know where to fetch your data from |
| API secret (stored only as a SHA-1 hash, the form Nightscout itself expects) or access token / Nocturne `noc_` key | To authenticate to your site on your behalf |
| Display units, glucose thresholds, default graph window, timezone | To format your graphs and stats |
| Privacy setting (Public / Private) | To decide whether others may use `/ns check-on` on you |
| Graph preferences (colors, colorblind mode, theme, y-axis scaling, basal display, minimum bolus) | To style your graphs |
| Created / updated timestamps | Basic record-keeping |

**Please note:** access tokens are stored as entered, and an API secret hash
is enough to authenticate to your Nightscout site. Wherever possible, use a
read-only Nightscout access token rather than your API secret, so the Bot
never holds credentials that can change your data.

### Information fetched from your Nightscout site

When you run a command, the Bot uses your stored credentials to read data
from your Nightscout site, which may include glucose readings, treatments
(insulin doses, carbs), basal profiles, device status (uploader and pump
battery, reservoir, loop status, IOB), and site/sensor change events.

This data is processed **in memory only** to produce the requested image or
message. It is not written to the Bot's database or kept after the reply is
sent, except as described under [Logs](#logs).

### Information from Discord

Discord provides the Bot with basic information needed to respond to
commands, such as your user ID, display name, and the server and channel a
command was used in. The Bot does not request any privileged intents and
does not read message content.

### Logs

The Bot keeps operational logs to diagnose problems. Logs may include your
Discord user ID and error details when a request to your Nightscout site
fails. They are not used for any other purpose and are not shared, other
than with our hosting provider as part of normal operation.

## How we use information

We use your information only to:

- Provide the Bot's features (graphs, stats, status, `/ns check-on`);
- Troubleshoot errors and keep the Bot running; and
- Comply with legal obligations.

We do **not** sell your information, use it for advertising, use it to train
machine-learning models, or combine it with data from other sources.

## Who can see your data

- **Private replies.** Setup, configuration, and settings commands reply
  privately (only you can see the reply).
- **Public replies.** Commands such as `/ns graph`, `/ns now`, `/ns agp`,
  `/ns tdd`, `/ns tir`, `/ns averages`, `/ns gmi`, `/ns status` and
  `/ns ages` post their results **in the channel**, where anyone who can read
  that channel can see them. Once posted, those messages are stored by
  Discord and are subject to [Discord's Privacy Policy](https://discord.com/privacy),
  not this one. You can delete them yourself through Discord.
- **`/ns check-on`.** If you set `/ns privacy` to **Public**, other members
  of any server you share with the Bot can view your latest reading with
  `/ns check-on`. The default is **Private**, which blocks this.

Think about who can read a channel before running a public command there.
Health information shared in a Discord channel may be visible to people you
don't know.

## Sharing with third parties

We share information only with:

- **Discord**, to deliver the Bot's replies;
- **Your own Nightscout / Nocturne site**, which the Bot contacts on your
  behalf using the URL and credentials you provided; and
- **Our hosting provider**, which stores the Bot's database and logs on its
  servers.

We may also disclose information if required by law or to protect the
safety of users or the public.

## Data retention and deletion

Your settings are kept until you delete them. Run **`/ns clear`** at any time
to permanently delete your stored configuration, including your site URL and
credentials, from the Bot's database. Copies may persist briefly in backups
or logs before they are rotated out.

If you can't use the command (for example, because you've left every server
the Bot is in), contact us using the details below and we will delete your
data.

You can also revoke the Bot's access at any time by deleting or rotating the
access token or API secret on your Nightscout site.

## Security

We take reasonable measures to protect stored data, including keeping the
database out of source control and limiting access to the host. No system is
perfectly secure, however, and we cannot guarantee the security of your
information. Using a read-only access token limits what anyone could do with
your credentials if they were ever exposed.

## Children

The Bot is not directed at children under 13 (or the minimum age required to
use Discord in your country). If you are under the age of majority where you
live, use the Bot only with the involvement of a parent or guardian.

## Your rights

Depending on where you live, you may have rights to access, correct, delete,
or export your personal data, or to object to its processing. You can view
your settings with `/ns config`, change them with the `/ns` commands, and
delete them with `/ns clear`. For any other request, contact us.

## Self-hosted instances

The Bot is open-source software. This policy covers only the instance we
operate. If you use a copy run by someone else, that operator is responsible
for its data handling and should provide their own policy.

## Changes to this policy

We may update this policy from time to time. Changes take effect when they
are posted to this file, and the "Last updated" date above will change.
Continued use of the Bot after a change means you accept the updated policy.

## Contact

Questions or requests about this policy: **[beetboop@poohstrnak.com](mailto:beetboop@poohstrnak.com)**
