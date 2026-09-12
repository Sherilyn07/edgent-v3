import { useState } from 'react'

const ACCEPTED = '.pdf,.docx,.txt,.md,.mp4,.mov,.mkv,.webm,.mp3,.m4a,.wav'

export default function UploadPanel({ onStart }) {
  const [files, setFiles] = useState([])
  const [dragging, setDragging] = useState(false)

  // Add to what is already there rather than replacing it, so dropping a
  // second batch does not silently throw the first one away. Same filename
  // twice counts once.
  function addFiles(list) {
    setFiles((current) => {
      const seen = new Set(current.map((file) => file.name))
      return [...current, ...Array.from(list).filter((file) => !seen.has(file.name))]
    })
  }

  function removeFile(name) {
    setFiles((current) => current.filter((file) => file.name !== name))
  }

  return (
    <section className="card">
      <div
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          addFiles(e.dataTransfer.files)
        }}
      >
        <p className="dropzone-title">Drop files here</p>
        <p className="hint">PDF, Word, text, audio or video</p>
        <label className="filebutton">
          <span>Choose files</span>
          <input type="file" multiple accept={ACCEPTED} onChange={(e) => addFiles(e.target.files)} />
        </label>
      </div>

      {files.length > 0 && (
        <ul className="filelist">
          {files.map((file) => (
            <li key={file.name}>
              <span className="name">{file.name}</span>
              <span className="muted">{(file.size / 1e6).toFixed(1)} MB</span>
              <button className="ghost" onClick={() => removeFile(file.name)} aria-label={`Remove ${file.name}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <button className="primary" disabled={files.length === 0} onClick={() => onStart(files)}>
        Upload and process
      </button>

      <p className="hint footnote">
        Files upload straight to storage. They never pass through the API.
      </p>
    </section>
  )
}
