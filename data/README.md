# Olist demo data notice

`olist_demo.sqlite` is a deterministic 500-order subset of the **Brazilian E-Commerce Public Dataset by Olist**, downloaded from the [official Kaggle dataset page](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce). It keeps the latest orders with source line items by purchase timestamp. The importer omits customer city/state/ZIP, reviews, free text, and geolocation. It retains source order, customer, product, and seller IDs plus order timestamps/status, item prices/freight, payment types/values/installments, product categories/weights, and seller state.

The dataset is attributed to Olist and licensed **CC BY-NC-SA 4.0**. This database subset is distributed under that same license, separately from the MIT-licensed application code. The license prohibits commercial use and requires attribution and ShareAlike for adaptations. Do not use this subset in a paid or otherwise commercial service without obtaining a suitable separate license. See the [license deed](https://creativecommons.org/licenses/by-nc-sa/4.0/) and the Kaggle page for the source notice.

This dataset contains historical, anonymized source facts from 2016–2018. ContextGraph's refund rule and tool orchestration are simulated; they are not Olist records or policies. The app does not persist source CSVs or customer location/review content.

To recreate the subset from the source archive:

```bash
python scripts/import_olist.py --archive data/raw/olist.zip --output data/olist_demo.sqlite --orders 500
```
