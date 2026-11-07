import { useState } from "react";

// A secret Argus keeps: once saved it says so plainly ("Saved, kept
// encrypted"), with Replace and Remove, instead of an empty-looking box whose
// placeholder says saved. Not saved: a field and Save.
export default function SecretField({ label, saved, placeholder, help, busy, onSave, onRemove, removeText = "Remove" }) {
  const [replacing, setReplacing] = useState(false);
  const [value, setValue] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    if (!value) return;
    if (await onSave(value) !== false) {
      setValue("");
      setReplacing(false);
    }
  };
  if (saved && !replacing) {
    return (
      <div className="field secret-field">
        <span>{label}</span>
        <span className="secret-saved">
          <span><span className="st-up">✓ Saved</span><span className="muted">, kept encrypted.</span></span>
          <button type="button" className="link-button" disabled={busy} onClick={() => setReplacing(true)}>Replace</button>
          {onRemove && <button type="button" className="link-button" disabled={busy} onClick={onRemove}>{removeText}</button>}
        </span>
        {help && <small>{help}</small>}
      </div>
    );
  }
  return (
    <form className="field secret-field" onSubmit={submit}>
      <span>{label}</span>
      <span className="field-row">
        <input type="password" value={value} onChange={(e) => setValue(e.target.value.trim())} autoComplete="off"
               placeholder={placeholder} aria-label={label} autoFocus={replacing} />
        <button className="btn" disabled={!value || busy}>Save</button>
        {replacing && <button type="button" className="btn" onClick={() => { setReplacing(false); setValue(""); }}>Cancel</button>}
      </span>
      {help && <small>{help}</small>}
    </form>
  );
}
