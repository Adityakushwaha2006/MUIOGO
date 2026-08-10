export class Model {

    constructor (countries, catalogSource) {
        // Country calibrations from the installer register, tagged with this
        // machine's install state. The og-core base package is not a country,
        // it is installed as a dependency, so it stays out of the grid.
        this.calibrations = (countries || []).filter(c => !c.is_base);
        // live | cache | none: how the catalogue was obtained
        this.catalogSource = catalogSource || 'none';
        this.pageID = 'OGCore';
    }
}
