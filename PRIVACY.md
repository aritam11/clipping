# Privacy Policy – Clipper

_Last updated: 21 November 2026_

Clipper is an open-source tool that runs entirely on the user's own computer. It turns public livestreams and videos into short clips and can upload those clips to the user's own YouTube channel.

## Google user data Clipper accesses

When you connect a YouTube channel (`clip youtube auth`), Clipper asks Google for two permissions:

- **`youtube.upload`**: to upload the clips you make to your own channel.
- **`youtube.readonly`**: to read your channel's name (shown when you sign in) and to check the privacy status of videos Clipper uploaded.

Clipper doesn't read, change or delete anything else on your Google account or channel.

## How that data is used and stored

- The sign-in token Google issues is stored **only on your computer**, in `credentials/youtube_token.json`. It is never sent anywhere except to Google's own servers, which use it to authorize uploads.
- Clipper keeps a local record of what it uploaded (`work/uploads.json`) so it never uploads the same clip twice. This stays on your computer.
- The developer of Clipper receives **no** data from you. There are no servers, accounts, analytics or tracking.
- Your Google user data is never sold, shared or transferred to anyone. It isn't used for advertising, and it isn't used to train any AI or machine-learning model.

Clipper's use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

## Other services Clipper uses

To decide which moments to clip, Clipper can send the **transcript of the public stream being clipped** to Anthropic's Claude, either through your own Claude Code login or your own Anthropic API key. No Google account data is ever included. Downloads of public videos go directly from YouTube, Twitch or other sites to your computer.

## Removing access

- Revoke Clipper's access at any time at <https://myaccount.google.com/permissions>.
- Delete the `credentials/` folder in your Clipper folder to remove the stored token from your computer.

## Contact

Questions about this policy: open an issue at <https://github.com/aritam11/clipping/issues>.
