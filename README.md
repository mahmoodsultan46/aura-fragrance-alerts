# Aura Fragrance new-arrival alerts

Checks the entire [Men's New Arrivals collection](https://www.aurafragrance.com/collections/mens-new-arrivals) on a five-minute GitHub Actions schedule and sends newly seen products to a Discord channel. Runs in GitHub's cloud while your computer is off. Python standard library only; no AI/API subscription.

## What “new only” means

- The first successful run silently saves all current products. It sends no product alerts.
- After that, each previously unseen product ID gets an alert with its name and product link, including new listings that are already sold out.
- Price/stock/name changes, reorderings, removals, and returning previously seen IDs do not send alerts. Men's and unisex listings in this collection are included.
- An old item newly added to this collection is a new collection listing and can alert. A shop recreating a product under a different ID also appears new.
- History is permanent in `state/seen.json`; do not delete or reset it.

## Set up once

1. Create a **public** GitHub repository, for example `aura-fragrance-alerts`. Public is required for unlimited free standard GitHub-hosted runner minutes. The code and product history are public; the webhook stays secret.
2. Add this folder's files, including `.github/workflows/monitor.yml`, to its default branch.
3. In Discord, open **Server Settings → Integrations → Webhooks**, create one for your alert channel, and copy its URL. You need Manage Webhooks permission. This posts into a server channel, not a direct message.
4. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**. Name it `DISCORD_WEBHOOK_URL`; paste the webhook as the value. Never put the URL in source code, an issue, or the README.
5. Open **Actions → Aura new arrivals → Run workflow**. The first run creates the baseline without alerts. Check its log says `Baseline saved` and the new `state/seen.json` commit exists.
6. Run the workflow once more. It should report zero new-product alerts. Future scheduled runs will alert for new IDs.

To receive push notifications for channel messages, set the Discord alert channel's notification setting to **All Messages** and make sure it is not muted. The monitor does not ping everyone or require a Discord bot.

If your repository or organization prevents Actions from writing commits, allow this workflow's `contents: write` permission or use a dedicated repository without protected-default-branch restrictions.

## Cost and timing

[Standard GitHub Actions runners are free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions). This workflow uses no paid runners, caches, artifacts, AI services, or external monitoring subscriptions. Private repositories have limited included minutes and are not the recommended free setup at this cadence.

[GitHub allows a five-minute minimum schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), but scheduled jobs can be delayed or dropped. This is a five-minute requested schedule, not a promise of an alert within five minutes. A listing that appears and disappears between successful scans can be missed. GitHub can disable public scheduled workflows after 60 days without repository activity; the monitor writes one successful-check date per UTC day so successful runs create activity without committing every poll. Check Actions occasionally and re-enable a disabled workflow if necessary.

Aura may throttle or change its feed. Failed or malformed scans preserve the history and send no product alerts. A failed workflow appears in Actions; GitHub account notification settings control failure notifications. Discord alerts are for new products only.

## Delivery and recovery

The script commits a pending-alert outbox before sending and removes each alert only after Discord confirms delivery. Failed deliveries remain pending for the next successful run. A crash or push failure after Discord accepts a message but before its acknowledgment is committed can result in a duplicate; Discord webhooks do not provide a transactional exactly-once guarantee.

Concurrent monitor runs are serialized. Avoid editing `state/seen.json` manually or changing the default branch while a run is delivering alerts. A rejected state push stops the run and is visible as a workflow failure.

## Local checks

```sh
python3 -m unittest -v
python3 monitor.py --dry-run
```

The dry run reads every live page without saving history or contacting Discord.
