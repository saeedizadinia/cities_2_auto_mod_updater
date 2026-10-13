# Cities: Skylines II Manual Mod Updater (Windows)

A conservative updater for manually extracted folders such as `move-it_*` in the default Cities: Skylines II Mods folder. The program **does not subscribe to mods or alter Paradox playsets**.

## Important status

**This is a tested-offline beta, not a fully verified production deployment.** The Paradox Mods API and archive URL format are undocumented and subject to change; no live Windows/Paradox download or WinRAR integration was available during construction. Validate with a single mod and a separate test folder before using it with your real Mods directory. Always close the game first. Automatic scheduled checks are preview-only by default.

## Setup

1. Install Python 3.10+ for Windows. Check `py -3 --version`.
2. Double-click `Install_Requirements.bat`.
3. Double-click `Launch_GUI.bat`.
4. Default folder: `%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\Mods`.
5. Click **Auto-map IDs**, or simply click **Check Online (Preview)**. The updater searches Paradox Mods by folder name and saves only one unambiguous exact normalized-name match.
6. Review the matches. If a search is ambiguous, fails, or chooses the wrong mod, select the folder and click **Edit Mod ID** to paste its official Paradox Mods URL (`https://mods.paradoxplaza.com/mods/74324/Windows`) or numeric ID. **No update is performed without a matching numeric online revision.**
7. Close the game and click **Download & Install Updates**. Backups are saved outside the Mods folder.
8. Optional: `Install_Monthly_Task.bat` makes a preview-only task for the first day of each month at 10 AM. Use the GUI or `Install_Updates.bat` for actual installation.

## Detection and safety

- Only directories ending in `_NUMBER` are detected. E.g. `move-it_37` -> slug `move-it`, local revision `37`.
- Missing IDs are searched automatically by normalized folder/display name. Existing mappings are never overwritten, duplicate exact names remain unmapped, and the closest candidates and IDs are displayed for manual review.
- The API's `modDetail.modVersion` must be numeric, and `modDetail.displayName` must match the normalized folder name. The API's internal `name` value is used to construct the archive URL. Otherwise the updater skips it.
- Downloads come from Paradox's own content domain; HTML and JSON responses are refused. ZIP archives are checked and extracted safely with Python in a temporary directory; WinRAR is retained only as a fallback for non-ZIP archives.
- An original folder is moved to an application-specific backup before the replacement folder is promoted; failure to promote restores the original. Receipts and logs are written to `%LOCALAPPDATA%\CS2ManualModUpdater`.
- The most recent backups can be restored via **Rollback Selected** or `py -3 updater.py --rollback move-it`.
- Extracted content must not contain symlinks/junctions or traversal paths; size limits are enforced after extraction. Do not run on archives from untrusted sources.
- The application avoids touching folders it cannot confidently identify, but it cannot guarantee mod compatibility, dependency correctness, malware safety, or safe operation when the game is running.

## Manual commands

```powershell
py -3 updater.py                 # preview only
py -3 updater.py --apply         # apply updates
py -3 updater.py --rollback move-it
py -3 -m unittest discover -s tests -v
```

## Configuration

After first launch, edit `%LOCALAPPDATA%\CS2ManualModUpdater\config.json` or use the GUI mapping screen. Example:

```json
{
  "mods_dir": "C:\\Users\\YOUR_USER\\AppData\\LocalLow\\Colossal Order\\Cities Skylines II\\Mods",
  "winrar_path": "C:\\Program Files\\WinRAR\\WinRAR.exe",
  "mappings": {
    "move-it": {"mod_id": 74324},
    "another-mod": {"mod_id": 123456, "source": "manual"}
  },
  "keep_backups": 3
}
```

## Known limitations

- Renamed, removed, or ambiguously named mods may still require manual ID mapping. Manual mappings intentionally override the normal folder/display-name check and are identified in preview results.
- The API endpoint and the content URL template are unofficial; test against live service. Public mods are assumed; authentication is not implemented.
- If the online mod name differs from the local folder slug, the mod is intentionally skipped. You may need an explicit alias extension.
- This does not maintain mod dependencies, config migrations, or compatibility with a specific game build.
- Large archive detection and non-ZIP WinRAR fallback handling are conservative and may require site-specific adjustment.
- It does not check whether the game is running. **Close it before updating.**
- Rolling back leaves the newer folder parked for investigation, rather than deleting it.

The reverse-engineered API approach was documented by [Cs2ModsCLI](https://github.com/sakayase/Cs2ModsCLI), and the individual-mod details/download pattern by a [Paradox Mods download userscript](https://greasyfork.org/en/scripts/524357). Neither is an official published API contract.
