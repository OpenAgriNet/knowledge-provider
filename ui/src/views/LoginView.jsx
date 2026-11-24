import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { AuthLoadingScreen, InlineSpinner } from '../components/AuthLoadingScreen'
import { HeroPanel } from '../components/HeroPanel'
import { PlatformLogoIcon } from '../components/PlatformLogoIcon'
import { Button } from '../components/ui/button'
import { useAuth } from '../auth/AuthProvider'
import {
  AUTH_ENABLED,
  ROUTES,
  finishGoogleSignIn,
  googleSignInEnabled,
  requestEmailCode,
  startGoogleSignIn,
  verifyEmailCode,
} from '../auth/keycloak'
import { APP_DESCRIPTION, APP_NAME } from '../lib/app-brand'

/** Google's brand mark — kept as the official four-colour glyph. */
function GoogleIcon({ className }) {
  return (
    <svg viewBox="0 0 24 24" className={className ?? 'size-5'} aria-hidden="true">
      <path
        fill="#4285F4"
        d="M23.52 12.27c0-.82-.07-1.6-.21-2.36H12v4.47h6.46a5.52 5.52 0 0 1-2.4 3.62v3h3.87c2.27-2.09 3.58-5.17 3.58-8.73Z"
      />
      <path
        fill="#34A853"
        d="M12 24c3.24 0 5.96-1.08 7.94-2.91l-3.88-3c-1.07.72-2.45 1.15-4.06 1.15-3.13 0-5.78-2.11-6.73-4.95H1.26v3.09A12 12 0 0 0 12 24Z"
      />
      <path
        fill="#FBBC05"
        d="M5.27 14.29a7.2 7.2 0 0 1 0-4.58V6.62H1.26a12 12 0 0 0 0 10.76l4.01-3.09Z"
      />
      <path
        fill="#EA4335"
        d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.44-3.44C17.95 1.18 15.23 0 12 0A12 12 0 0 0 1.26 6.62l4.01 3.09C6.22 6.86 8.87 4.75 12 4.75Z"
      />
    </svg>
  )
}

export default function LoginView() {
  const { isAuthenticated, isInitializing, bootstrapped, signInWithSession } = useAuth()
  const [email, setEmail] = useState('')
  const [challenge, setChallenge] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showGoogle, setShowGoogle] = useState(false)

  useEffect(() => {
    if (!AUTH_ENABLED || isAuthenticated) return
    let active = true
    googleSignInEnabled().then(enabled => {
      if (active) setShowGoogle(enabled)
    }).catch(() => {})
    return () => { active = false }
  }, [isAuthenticated])

  useEffect(() => {
    if (!bootstrapped || isAuthenticated) return
    const result = window.location.hash
    if (result !== '#google-complete' && result !== '#google-error') return
    const clearResult = () => window.history.replaceState(
      window.history.state, '', window.location.pathname + window.location.search,
    )
    if (result === '#google-error') {
      setError('Google sign-in could not be completed. Try again or use email.')
      clearResult()
      return
    }
    let active = true
    setBusy(true)
    finishGoogleSignIn().then(async tokens => {
      if (!active) return
      clearResult()
      await signInWithSession(tokens)
    }).catch(failure => {
      if (!active) return
      clearResult()
      setError(failure.message)
    }).finally(() => {
      if (active) setBusy(false)
    })
    return () => { active = false }
  }, [bootstrapped, isAuthenticated, signInWithSession])

  if (AUTH_ENABLED && (!bootstrapped || isInitializing)) {
    return <AuthLoadingScreen title="Welcome back…" message="Restoring your session" />
  }
  if (isAuthenticated) return <Navigate to={ROUTES.HOME} replace />

  const send = async () => {
    setBusy(true)
    setError('')
    try {
      const result = await requestEmailCode(email.trim())
      setChallenge(result.challenge)
      setCode('')
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  const submit = async (event) => {
    event.preventDefault()
    if (!challenge) {
      await send()
      return
    }
    setBusy(true)
    setError('')
    try {
      await signInWithSession(await verifyEmailCode(challenge, code))
    } catch (failure) {
      if (failure.challenge) setChallenge(failure.challenge)
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  const signInWithGoogle = async () => {
    setBusy(true)
    setError('')
    try {
      await startGoogleSignIn()
    } catch (failure) {
      setError(failure.message)
      setBusy(false)
    }
  }

  const changeEmail = () => {
    setChallenge('')
    setCode('')
    setError('')
  }

  return (
    <div className="grid min-h-svh lg:grid-cols-[1fr_minmax(0,40rem)]">
      <HeroPanel />

      {/* One centred column shared by brand, form and footer, so all three keep
          the same left and right edge and the panel's padding stays symmetric. */}
      <main className="flex min-h-svh w-full flex-col bg-canopy-form-panel px-6 text-canopy-form-text sm:px-10">
        <div className="mx-auto flex w-full max-w-[26rem] flex-1 flex-col">
          {/* Brand sits inline at the top on small screens, where the hero
              panel is hidden and would otherwise leave it unbranded. */}
          <header className="flex items-center gap-3 py-8 lg:hidden">
            <PlatformLogoIcon className="size-9 rounded-xl shadow-sm" title={APP_NAME} />
            <span className="text-sm font-semibold tracking-tight">{APP_NAME}</span>
          </header>

          <div className="flex flex-1 flex-col justify-center py-10">
            <h1 className="text-[2.125rem] font-semibold leading-[1.15] tracking-tight text-canopy-form-text">
              {challenge ? 'Check your email' : 'Sign in'}
            </h1>
            <p className="mt-3 text-[0.9375rem] leading-relaxed text-canopy-form-muted">
              {challenge ? `Enter the six-digit code sent to ${email.trim()}.` : APP_DESCRIPTION}
            </p>

            {error ? (
              <div
                role="alert"
                className="mt-6 border-l-2 border-red-500 bg-red-50/60 py-2.5 pl-4 pr-3 text-sm leading-relaxed text-red-700"
              >
                {error}
              </div>
            ) : null}

            {showGoogle && !challenge ? (
              <div className="mt-8">
                <Button
                  type="button"
                  size="lg"
                  disabled={busy}
                  onClick={() => void signInWithGoogle()}
                  className="h-[3.25rem] w-full gap-3 rounded-lg border border-canopy-form-border bg-white text-[0.9375rem] font-medium text-[#3c4043] shadow-sm transition-colors hover:bg-neutral-50 disabled:opacity-70"
                >
                  {busy ? <InlineSpinner /> : <GoogleIcon className="size-5" />}
                  Continue with Google
                </Button>

                <div className="mt-7 flex items-center gap-4" aria-hidden="true">
                  <span className="h-px flex-1 bg-canopy-form-border" />
                  <span className="text-[0.6875rem] font-medium uppercase tracking-[0.14em] text-canopy-form-muted">
                    or
                  </span>
                  <span className="h-px flex-1 bg-canopy-form-border" />
                </div>
              </div>
            ) : null}

            <form onSubmit={submit} className={`${showGoogle && !challenge ? 'mt-7' : 'mt-8'} space-y-3`}>
              {challenge ? (
                <div key="code">
                  <label htmlFor="sign-in-code" className="mb-2 block text-sm font-medium">One-time code</label>
                  <input
                    id="sign-in-code"
                    value={code}
                    onChange={(event) => setCode(event.target.value.replace(/[^0-9]/g, '').slice(0, 6))}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    pattern="[0-9]{6}"
                    required
                    autoFocus
                    className="h-[3.25rem] w-full rounded-lg border border-canopy-form-muted/50 bg-white px-3 text-slate-900"
                  />
                </div>
              ) : (
                <div key="email">
                  <label htmlFor="sign-in-email" className="mb-2 block text-sm font-medium">Email address</label>
                  <input
                    id="sign-in-email"
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    autoComplete="email"
                    maxLength={254}
                    required
                    autoFocus
                    className="h-[3.25rem] w-full rounded-lg border border-canopy-form-muted/50 bg-white px-3 text-slate-900"
                  />
                </div>
              )}
              <Button
                type="submit"
                size="lg"
                disabled={busy}
                className="h-[3.25rem] w-full rounded-lg bg-canopy-form-accent text-[0.9375rem] font-medium text-canopy-form-accent-foreground shadow-sm transition-colors hover:bg-canopy-form-accent-hover"
              >
                {challenge ? (busy ? 'Working…' : 'Sign in') : (busy ? 'Sending…' : 'Send code')}
              </Button>
              {challenge ? (
                <div className="flex justify-between gap-4">
                  <Button type="button" variant="link" disabled={busy} onClick={changeEmail}>Change email</Button>
                  <Button type="button" variant="link" disabled={busy} onClick={() => void send()}>Resend code</Button>
                </div>
              ) : null}
            </form>
          </div>

          <footer className="py-8 text-xs leading-relaxed text-canopy-form-muted">
            Secured by your organization’s identity provider
          </footer>
        </div>
      </main>
    </div>
  )
}
