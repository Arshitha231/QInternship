// A stable hue per person, derived from their name.
//
// Decorative, but not only decorative: in a directory every monogram is two
// grey letters in an identical circle, and a consistent colour makes a face
// you have seen before findable in a list. The same person gets the same hue
// on their profile, in search results, in the org tree and in the top bar,
// which is the whole point — a random or index-based colour would change as
// results reorder and would be worse than no colour at all.
//
// Hues are pulled toward the plum end of the wheel rather than spanning it,
// so a page of avatars still reads as one palette instead of a paint chart.
// FNV-1a because it is short, stable across runs, and has no dependencies —
// this is not a security hash and nothing depends on it being one.
export function avatarHue(name: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  // 250-330deg: violet through plum to rose. Narrow on purpose.
  return 250 + (Math.abs(h) % 80);
}

export function avatarStyle(name: string): React.CSSProperties {
  return { ["--avatar-hue" as string]: String(avatarHue(name)) };
}
