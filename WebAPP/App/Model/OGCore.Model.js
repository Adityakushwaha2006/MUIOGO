export class Model {

    constructor (countries, catalogSource, installed) {
        //the og-core base package is a dependency, not a country, keep it out
        let catalog = (countries || []).filter(c => !c.is_base);

        //custom calibrations live in the machine registry but not in the
        //register, merge them in so every registered calibration has a card
        let known = {};
        $.each(catalog, function (id, c) { known[c.country_id] = true; });
        $.each(installed || [], function (id, r) {
            if (!known[r.country_id]) {
                catalog.push({
                    country_id: r.country_id,
                    country_name: r.country_name,
                    catalog_key: null,
                    repo_url: r.repo_url || '',
                    install_state: r.install_state || 'installed',
                    install_id: r.install_id || null,
                    is_base: false,
                    custom: true
                });
            }
        });

        this.calibrations = catalog;
        //registry records by country_id (source_type, paths) for retry/update
        this.records = {};
        let records = this.records;
        $.each(installed || [], function (id, r) { records[r.country_id] = r; });
        //live | cache | none: how the catalogue was obtained
        this.catalogSource = catalogSource || 'none';
        this.pageID = 'OGCore';
    }
}
