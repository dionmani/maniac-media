/**
 * Copy text to the clipboard. `navigator.clipboard` only exists in secure contexts (HTTPS or
 * localhost), and self-hosters usually browse to http://<lan-ip>, so fall back to the legacy
 * hidden-textarea + execCommand path there.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // permission denied or document not focused: try the fallback
    }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  // inside an open MUI dialog the focus trap pulls focus back from <body>, so attach to the active dialog
  const host = (document.activeElement?.closest('[role="dialog"]') as HTMLElement | null) ?? document.body;
  host.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  host.removeChild(area);
  return ok;
}
