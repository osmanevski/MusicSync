---
name: apple-music-esle
description: "Spotify → Apple Music senkronunda ISRC ile eşleşmeyen ya da ISRC'si şüpheli şarkıları sen eşle. 'Apple kuyruğu', 'Apple eşleşmeleri', 'Apple'a geçmeyenler', haftalık/istek üzerine inceleme ve gece senkronundan sonra kullan."
version: 1.0.0
author: Osman
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Music Sync, Apple Music, Spotify, ISRC, Eşleştirme]
---

# Apple Music eşleştirme

Music Sync her gece 03:30'da Osman'ın takip ettiği Spotify listelerini Apple
Music'teki aynı adlı listelere taşır. Şarkıyı **ISRC** ile (kaydın evrensel
kimlik kodu) birebir bulur. Şu durumlarda şarkı yazılmaz, **senin kuyruğuna** düşer:

- `ISRC yok` / `ISRC Apple'da bulunamadi`: Apple'da aynı kodlu kayıt yok.
- `ISRC ayni ama sure farki N sn` ya da `isim ve sanatci tutmuyor`: kod aynı ama
  Apple'daki kayıt bariz başka bir şarkı. Kodu güvenme, sen karar ver.

Senin onayın **"aynı şarkı" demektir**: o eşleşme kaydedilir, şarkının beklediği
bütün Apple listelerine eklenir ve ileride iki yön de bu eşleşmeyi kullanır.
**Apple'a eklenen şarkı API ile geri alınamaz.** Emin değilsen onaylama.

## Araç

```bash
sudo apple-esle kuyruk                          # bekleyenler, JSON
sudo apple-esle ara "<şarkı sanatçı>"           # Apple kataloğunda ara, JSON
sudo apple-esle onayla <spotify_id> <apple_id>  # aynı şarkı say, listelere ekle
sudo apple-esle yok <spotify_id>                # Apple'da karşılığı yok
sudo apple-esle geri <spotify_id>               # 'yok' kararını geri al (sonraki gece yeniden aranır)
```

`kuyruk` her şarkı için Spotify adı, sanatçı, süre (sn), ISRC, sebep ve en iyi 3
Apple adayını (`am_id`, ad, sanatçı, süre, puan) verir.

## Nasıl karar verirsin

Aynı şarkı = **aynı sanatçının aynı eseri, aynı kaydı ya da kulakta aynı duran
yayını**. Sırayla bak:

1. **Sanatçı** aynı kişi/grup mu? Yazım farkı (Türkçe harf, "feat.", "&", ikinci
   sanatçının eksik olması) sorun değil. Başka biri ise (cover, başka yorumcu)
   onaylama.
2. **Ad** aynı eser mi? Parantez içi ekler (`Remastered`, `2021 Remaster`, alt
   başlık, film adı) aynı kayıt sayılır. `Canlı`/`Live`, `Remix`, `Akustik`,
   `Version 2`, `Enstrümantal` farklı kayıttır: Spotify tarafı da aynı ekle
   değilse onaylama.
3. **Süre**: 5 sn'ye kadar fark normal; 6–15 sn remaster/kesim farkı olabilir,
   ad ve sanatçı tam tutuyorsa onayla; 15 sn üstü genelde başka kayıt.
4. Adaylar yetmiyorsa `ara` ile kendin ara (önce "ad sanatçı", sonra yalnız ad).
5. Hiçbir aday tutmuyorsa: en az iki farklı aramada (ad + sanatçı, yalnız ad)
   o sanatçının kaydı çıkmıyorsa `yok`. Remix/edit/bağımsız yükleme için de
   aynı. Emin değilsen **dokunma**, Osman'a sor. `yok` kalıcıdır; Osman itiraz
   ederse `geri` ile aç.

ISRC şüpheli kayıtlarda adaylar çoğu zaman yanlış şarkıdır (aynı albümden başka
parça). Doğrusunu `ara` ile bul.

## Sınırlar

- Yalnız bu komutlar. Spotify'a, YouTube'a ya da Apple'a başka yoldan yazma.
- Spotify listesini değiştirme; şarkıyı Osman ekler.
- Apple listesinden silme ve sıralama yapılamaz (Apple'ın API'si izin vermiyor).
- Bir turda en fazla 50 onay; bitince Osman'a kısa rapor: kaç onay, kaç `yok`,
  kaç tanesi ona kaldı (adlarıyla).
