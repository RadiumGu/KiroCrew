/**
 * The crew webview's containment LABEL must not overclaim.
 *
 * The bar next to the panel says what containment the frame has. It previously
 * read "no network", which is false: the document loads the pinned Tailwind
 * runtime, so a user with devtools open sees a request the label denies. An
 * inaccurate security label is worse than a vague one — someone who catches one
 * false claim has no reason to believe the true ones beside it.
 *
 * So this pins the copy to a CODE fact rather than to a wording: as long as the
 * composed document references a fetched script, the label may not assert that
 * there is no network. It is deliberately a NEGATIVE check — the positive wording
 * stays free to improve without a test to update, while the specific error that
 * shipped cannot come back.
 */
import { describe, it, expect } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { TAILWIND_RUNTIME_PATH } from '../lib/vendorPaths'
import en from '../i18n/locales/en.json'

/** Every locale's copy of the containment detail. */
const CATALOGS = import.meta.glob<{ default: Record<string, unknown> }>(
  '../i18n/locales/*.json',
  { eager: true },
)

function detailOf(catalog: Record<string, unknown>): string | undefined {
  const pages = catalog.pages as Record<string, Record<string, string>> | undefined
  return pages?.membersPage?.webview_contained_detail
}

/**
 * Claims of ABSENT network activity, in every language this string ships in.
 * The label's TRUE claims — page isolation, blocked egress — are deliberately
 * not listed, so rewording them needs no change here.
 *
 * Non-ASCII patterns are written as \uXXXX escapes on purpose: the repo's
 * pre-push scrub refuses CJK characters in source files (locale JSON is exempt,
 * a .ts test is not). Escaping keeps the ja / ko / zh / hi / bn coverage, which
 * is exactly where a stale translation would otherwise hide. Do not inline them.
 */
const OVERCLAIMS = [
  /no network/i,
  /without network/i,
  /offline/i,
  /kein netzwerk/i,
  /ohne netzwerk/i,
  /sin red/i,
  /sans r\u00e9seau/i,
  /senza rete/i,
  /sem rede/i,
  /\u0431\u0435\u0437 \u0441\u0435\u0442\u0438/i,
  /\u30cd\u30c3\u30c8\u30ef\u30fc\u30af/,
  /\ub124\ud2b8\uc6cc\ud0ac/,
  /\u65e0\u7f51\u7edc/,
  /\u0928\u0947\u091f\u0935\u0930\u094d\u0915/,
  /\u09a8\u09c7\u099f\u09b0\u09cd\u0993\u09af\u09bc\u09be\u0995/,
]

describe('the containment label states containment accurately', () => {
  it('the frame really does fetch a script, so an absent-network claim would be false', () => {
    const doc = buildSrcdoc({ html: '<div>x</div>', themeVars: {}, mode: 'dark' })

    // The premise this whole file rests on. If the panel ever stops loading a
    // remote script, this fails and the label is free to say so again.
    expect(doc).toContain(TAILWIND_RUNTIME_PATH)
  })

  it('the English label does not claim there is no network', () => {
    const detail = detailOf(en as unknown as Record<string, unknown>)
    expect(detail, 'the containment detail string is missing').toBeTruthy()
    for (const pattern of OVERCLAIMS) {
      expect(detail, `overclaims via ${pattern}`).not.toMatch(pattern)
    }
  })

  it('no TRANSLATION reintroduces the claim the English dropped', () => {
    // A translated catalog is where a corrected string quietly survives: the
    // English gets reviewed and the twelve translations do not get re-read.
    const checked: string[] = []
    for (const [path, mod] of Object.entries(CATALOGS)) {
      const detail = detailOf(mod.default)
      if (!detail) continue // en.manual.json and any partial catalog
      checked.push(path)
      for (const pattern of OVERCLAIMS) {
        expect(detail, `${path} overclaims via ${pattern}`).not.toMatch(pattern)
      }
    }
    // Guards against the loop silently matching nothing.
    expect(checked.length).toBeGreaterThan(5)
  })
})
