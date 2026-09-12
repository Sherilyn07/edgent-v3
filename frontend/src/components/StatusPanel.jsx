const FILE_LABEL = {
  pending: 'queued',
  processing: 'reading…',
  done: 'ready',
  failed: 'failed',
}

const HEADING = {
  uploading: 'Uploading',
  processing: 'Reading your files',
  failed: 'Something went wrong',
}

export default function StatusPanel({ phase, status, progress, onReset }) {
  const uploads = Object.entries(progress)

  return (
    <section className="card">
      <div className="card-head">
        <h2>{HEADING[phase]}</h2>
        {phase !== 'uploading' && (
          <button className="ghost" onClick={onReset}>
            Start over
          </button>
        )}
      </div>

      {phase === 'processing' && (
        <p className="hint">
          Converting documents and transcribing video takes a while, especially the first time —
          the models have to download before they can run.
        </p>
      )}

      {phase === 'uploading' && (
        <ul className="filelist">
          {uploads.map(([filename, percent]) => (
            <li key={filename}>
              <span className="name">{filename}</span>
              <span className="bar">
                <span className="bar-fill" style={{ width: `${percent}%` }} />
              </span>
              <span className="muted pct">{percent}%</span>
            </li>
          ))}
        </ul>
      )}

      {phase !== 'uploading' && status?.files?.length > 0 && (
        <ul className="filelist">
          {status.files.map((file) => (
            <li key={file.file_id}>
              <span className="name">{file.filename}</span>
              <span className={`pill ${file.status}`}>
                {FILE_LABEL[file.status] || file.status}
                {file.chunk_count > 0 && ` · ${file.chunk_count} chunks`}
              </span>
              {file.error && <span className="rowerror">{file.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {phase === 'processing' && (
        <p className="hint footnote">
          Nothing here times out. If a service is switched off, this waits for ever.
        </p>
      )}
    </section>
  )
}
