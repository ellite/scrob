// Original languages offered by the Explore filters: [TMDB code, English name].
// "cn" is TMDB's code for Cantonese; Intl calls it "yue".
import { localizedLanguageName } from "./labels";

const EXPLORE_LANGUAGES: [string, string][] = [
  ["ar", "Arabic"], ["bn", "Bengali"], ["bg", "Bulgarian"], ["cn", "Cantonese"],
  ["zh", "Chinese"], ["cs", "Czech"], ["da", "Danish"], ["nl", "Dutch"],
  ["en", "English"], ["fi", "Finnish"], ["fr", "French"], ["de", "German"],
  ["el", "Greek"], ["gu", "Gujarati"], ["he", "Hebrew"], ["hi", "Hindi"],
  ["hu", "Hungarian"], ["is", "Icelandic"], ["id", "Indonesian"], ["it", "Italian"],
  ["ja", "Japanese"], ["kn", "Kannada"], ["ko", "Korean"], ["ms", "Malay"],
  ["ml", "Malayalam"], ["mr", "Marathi"], ["no", "Norwegian"], ["fa", "Persian"],
  ["pl", "Polish"], ["pt", "Portuguese"], ["pa", "Punjabi"], ["ro", "Romanian"],
  ["ru", "Russian"], ["si", "Sinhala"], ["es", "Spanish"], ["sw", "Swahili"],
  ["sv", "Swedish"], ["tl", "Tagalog"], ["ta", "Tamil"], ["te", "Telugu"],
  ["th", "Thai"], ["tr", "Turkish"], ["uk", "Ukrainian"], ["ur", "Urdu"],
  ["vi", "Vietnamese"],
];

export function exploreLanguageOptions(): { value: string; label: string }[] {
  return EXPLORE_LANGUAGES.map(([value, english]) => ({
    value,
    label: localizedLanguageName(value === "cn" ? "yue" : value, english),
  }));
}
