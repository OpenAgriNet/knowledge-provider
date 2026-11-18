import { Input } from '../components/ui/input'
import { Textarea } from '../components/ui/textarea'

export default function IntakeProfileFields({ profile, values, onChange, disabled }) {
  return (
    <fieldset className="grid gap-4 sm:grid-cols-2" disabled={disabled}>
      <legend className="mb-3 text-sm font-medium">Document attributes</legend>
      {Object.entries(profile.properties).map(([key, schema]) => {
        const { type, enum: options, title, description } = schema || {}
        const label = title || key.replaceAll('_', ' ')
        const required = profile.required.includes(key)
        const props = { value: values[key] ?? '', onChange: event => onChange(key, event.target.value) }
        const scalarOptions = options?.every(value => value !== null && typeof value !== 'object')
        return (
          <label key={key} className="grid min-w-0 content-start gap-1.5 text-sm">
            <span>{label}{required && <span className="ml-1 text-muted-foreground">(required)</span>}</span>
            {scalarOptions || type === 'boolean' ? (
              <select className="h-10 w-full rounded-md border border-input bg-background px-3" {...props}>
                <option value="">Select a value</option>
                {(scalarOptions ? options : [true, false]).map(value => (
                  <option key={JSON.stringify(value)} value={JSON.stringify(value)}>{String(value)}</option>
                ))}
              </select>
            ) : type === 'string' || type === 'integer' || type === 'number' ? (
              <Input type={type === 'string' ? 'text' : 'number'} step={type === 'number' ? 'any' : undefined} {...props} />
            ) : (
              <Textarea placeholder="Enter a JSON value" {...props} />
            )}
            {description && <span className="text-xs leading-relaxed text-muted-foreground">{description}</span>}
          </label>
        )
      })}
    </fieldset>
  )
}
