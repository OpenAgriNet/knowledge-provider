import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { AuthLoadingScreen } from '../components/AuthLoadingScreen'
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

            <form onSubmit={submit} className="mt-8 space-y-3">
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
            {showGoogle && !challenge ? (
              <Button
                type="button"
                variant="outline"
                size="lg"
                disabled={busy}
                onClick={() => void signInWithGoogle()}
                className="mt-3 h-[3.25rem] w-full rounded-lg border-canopy-form-muted/40 bg-transparent text-[0.9375rem]"
              >
                Continue with Google
              </Button>
            ) : null}
          </div>

          <footer className="py-8 text-xs leading-relaxed text-canopy-form-muted">
            Secured by your organization’s identity provider
          </footer>
        </div>
      </main>
    </div>
  )
}
