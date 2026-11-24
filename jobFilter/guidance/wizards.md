# Oracle HCM (`*.oraclecloud.com`), iCIMS, UltiPro, SuccessFactors, SmartRecruiters
- Wizards with an account or an email-only "apply" flow. Follow the same rules as Workday; report the exact step you stopped at.

## SmartRecruiters card sections

- The "Easy Apply" / `oneclick-ui` form is ONE page (Personal information, Experience, Education, Your Profiles, Resume, Message to the Hiring Team, then Next), not a wizard.
- Experience and Education start empty: a heading with an "+ Add" button and no fields. The card with the inputs appears only after that button is clicked. Clicking it once per record, filling the card and confirming it is required — an empty section is not "nothing to ask for". Observed 2026-11-24: both sections were left empty because no inputs were visible.
- "Your Profiles" holds LinkedIn/GitHub/portfolio URLs from the profile links.
- A blocked résumé upload does not excuse leaving the rest empty. Fill every section you can, then report the upload as the single remaining blocker.
- **Inputs live in a shadow root**, so `read_page` and `form_input` mostly miss them and clicking by coordinate (which needs a screenshot to aim) is the expensive fallback. Enumerate them through the shadow DOM first and work from refs:

  ```js
  function walk(root, out){ root.querySelectorAll('*').forEach(e=>{
    if(e.matches('input,textarea,select')) out.push(e);
    if(e.shadowRoot) walk(e.shadowRoot, out); }); return out; }
  walk(document, []).map(e => `${e.tagName} type=${e.type} name=${e.name} id=${e.id}`)
  ```

  Read values back the same way instead of screenshotting the section.
- **The résumé upload can succeed at the DOM level and still be ignored.** `file_upload` with a ref sets `input.files`, but a web component tracks its own state from events it dispatched itself, so the filename never appears and the field still reads as empty. Observed 2026-11-24: the upload ran against a valid ref, nothing showed, and the run concluded the input was unreachable — it was reachable, just unnoticed. After uploading, confirm the filename is visible. If it is not, notify the component from JS before giving up:

  ```js
  function walk(root, out){ root.querySelectorAll('*').forEach(e=>{
    if(e.matches('input,textarea,select')) out.push(e);
    if(e.shadowRoot) walk(e.shadowRoot, out); }); return out; }
  const el = walk(document, []).find(e => e.type === 'file');
  ['input','change'].forEach(t =>
    el.dispatchEvent(new Event(t, {bubbles: true, composed: true})));
  el.files.length    // confirm the file is actually attached
  ```

  `composed: true` is what lets the event cross the shadow boundary to the listener outside it; without it the component still sees nothing. Re-read the field afterwards, and only report the upload as blocked once this has failed too.

## Oracle address controls and efficient continuation

- Country/state/city/county/postal code may be dependent autocomplete controls. Inspect the dependencies; select country and state before city, then county/postal code as offered. A parent selection can clear child fields: wait for that update before filling them. Select the actual suggestion, then verify the stored label/value. Typed search text alone is not a selection.
- Use fresh labelled refs or supported form controls; batch independent fields. Read the page/field state after a meaningful change rather than taking repeated screenshots of unchanged content. Use screenshots when needed to resolve layout or selection ambiguity.
- After two unsuccessful attempts at the same control, inspect its current options and validation and change approach; if still blocked, report the exact control. Do not repeat a click/type/wait/screenshot loop until the turn budget is exhausted. Call only tools actually advertised by the session.
- On a turn-limit continuation, retain the same tab and saved work. Recheck the current step, fill only unfinished or incorrect fields, and perform the shared final verification. Never restart the entire application merely because a new attempt began.
