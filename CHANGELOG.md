# Changelog

## [0.1.1](https://github.com/pmontp19/ha-avisoscat/compare/ha-avisoscat-v0.1.0...ha-avisoscat-v0.1.1) (2026-08-19)


### Features

* add avisoscat warning notification blueprint ([#18](https://github.com/pmontp19/ha-avisoscat/issues/18)) ([8b43458](https://github.com/pmontp19/ha-avisoscat/commit/8b43458d16e6a979979ac3ae852200e5a7342fc4))
* **avisoscat:** add comarca reference table and point-in-polygon resolution ([#6](https://github.com/pmontp19/ha-avisoscat/issues/6)) ([d9daf20](https://github.com/pmontp19/ha-avisoscat/commit/d9daf206f2f0b7179ff63766d2763714a7c7df71))
* **avisoscat:** add diagnostics, resilience tracking and quota polling ([#19](https://github.com/pmontp19/ha-avisoscat/issues/19)) ([4160bc0](https://github.com/pmontp19/ha-avisoscat/commit/4160bc08c929e075d3a779822262e4e2eefbaf82))
* **avisoscat:** add options reload, reauth and reconfigure flows ([#17](https://github.com/pmontp19/ha-avisoscat/issues/17)) ([4be0ba2](https://github.com/pmontp19/ha-avisoscat/commit/4be0ba2eacb5f1d3a28948b00930b1d4ff580850))
* **avisoscat:** add per-meteor warning sensors ([#22](https://github.com/pmontp19/ha-avisoscat/issues/22)) ([4237652](https://github.com/pmontp19/ha-avisoscat/commit/42376523f805229c1df383f0e829c6db0aa97be9))
* **avisoscat:** add the binary sensor platform ([#16](https://github.com/pmontp19/ha-avisoscat/issues/16)) ([282756a](https://github.com/pmontp19/ha-avisoscat/commit/282756ac2ebbea67a976af5b3e09a0f543ec059b))
* **avisoscat:** add the config flow with comarca resolution ([#12](https://github.com/pmontp19/ha-avisoscat/issues/12)) ([de63e2f](https://github.com/pmontp19/ha-avisoscat/commit/de63e2fd52cc9792fb5516051afa2583e9f57f23))
* **avisoscat:** add the data coordinator and its six bus events ([#13](https://github.com/pmontp19/ha-avisoscat/issues/13)) ([394d636](https://github.com/pmontp19/ha-avisoscat/commit/394d6363069f0e916798677c1d727f8ecdc58c79))
* **avisoscat:** add the dual SMP data source behind one Protocol ([#10](https://github.com/pmontp19/ha-avisoscat/issues/10)) ([fc4108a](https://github.com/pmontp19/ha-avisoscat/commit/fc4108a612868f3924746379763df960469704a0))
* **avisoscat:** add the level sensor platform (T8) ([#15](https://github.com/pmontp19/ha-avisoscat/issues/15)) ([a41eb49](https://github.com/pmontp19/ha-avisoscat/commit/a41eb4983cfa12d837e94104cb9f71340cf21b48))
* **avisoscat:** add the SMP data model and its tolerant parser ([#7](https://github.com/pmontp19/ha-avisoscat/issues/7)) ([729b0ba](https://github.com/pmontp19/ha-avisoscat/commit/729b0ba5290812ae5b1b5e4703eeae7a799204f5))
* **avisoscat:** decide warning validity and the three-day outlook ([#11](https://github.com/pmontp19/ha-avisoscat/issues/11)) ([23ef4e7](https://github.com/pmontp19/ha-avisoscat/commit/23ef4e706a9a2165c1ac8d33e50a791ee4dfb3ed))
* **avisoscat:** extract the inline SMP payload from meteo.cat pages ([#8](https://github.com/pmontp19/ha-avisoscat/issues/8)) ([531bacc](https://github.com/pmontp19/ha-avisoscat/commit/531bacccf2a7de798e640cad8fe6936c2da1d4f8))
* **avisoscat:** scaffold the Home Assistant integration with CI and release automation ([#1](https://github.com/pmontp19/ha-avisoscat/issues/1)) ([e857daa](https://github.com/pmontp19/ha-avisoscat/commit/e857daa2bda965c6e3e3dee8694a19a7d9004a27))


### Bug Fixes

* **avisoscat:** read temps violent afectacions and stabilise payload hashing ([#9](https://github.com/pmontp19/ha-avisoscat/issues/9)) ([8897748](https://github.com/pmontp19/ha-avisoscat/commit/889774892bcb3086d3efa534d7e25363b8765847))
* **avisoscat:** wake PreavisSensor on a pre-warning change ([#24](https://github.com/pmontp19/ha-avisoscat/issues/24)) ([7b2aa00](https://github.com/pmontp19/ha-avisoscat/commit/7b2aa001c44257e08fb44401d156427716a8340f))


### Documentation

* add real-instance screenshots to README ([#26](https://github.com/pmontp19/ha-avisoscat/issues/26)) ([894a803](https://github.com/pmontp19/ha-avisoscat/commit/894a80305d8e7a931e10a11af7368004ee1481f4))
* add user-facing README for v1 launch ([#23](https://github.com/pmontp19/ha-avisoscat/issues/23)) ([57ac641](https://github.com/pmontp19/ha-avisoscat/commit/57ac6413d04879604ff3a657d0945c2ea646f5d4))
* remove CECAT fields from trap table + update HACS install path ([#25](https://github.com/pmontp19/ha-avisoscat/issues/25)) ([f2b528d](https://github.com/pmontp19/ha-avisoscat/commit/f2b528d37ef3f67d8aa671ed4b22b5662519c1c6))
* seed repository with the Meteocat SMP warnings design ([47a31e5](https://github.com/pmontp19/ha-avisoscat/commit/47a31e5a984170b40bb2b4437517962a9b9f0cf7))
