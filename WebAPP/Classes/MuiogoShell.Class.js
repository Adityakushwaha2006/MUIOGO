export class MuiogoShell {

    // Selected model for the whole shell: 'og', 'clews', or null (nothing picked
    // yet, home shows the model pick screen). Persisted so a reload keeps context.
    static getModel(){
        return localStorage.getItem('osy-model');
    }

    static setModel(model){
        if (model){
            localStorage.setItem('osy-model', model);
        }else{
            localStorage.removeItem('osy-model');
        }
    }

    // Body mode class drives all per-model chrome (sidebar sections, navbar
    // pieces, selector active state) from muiogo.css.
    static applyModel(){
        let model = MuiogoShell.getModel();
        $('body').removeClass('osy-mode-none osy-mode-og osy-mode-clews');
        if (model == 'og'){
            $('body').addClass('osy-mode-og');
        }else if (model == 'clews'){
            $('body').addClass('osy-mode-clews');
        }else{
            $('body').addClass('osy-mode-none');
        }
    }

    // Delegated so it works for the header buttons and the pick screen cards,
    // both of which load asynchronously.
    static initEvents(){
        $(document).off('click.osyModel');
        $(document).on('click.osyModel', '.osy-selectmodel', function(e){
            e.preventDefault();
            MuiogoShell.setModel($(this).attr('data-model'));
            MuiogoShell.applyModel();
            let hash = window.location.hash;
            if (hash == '' || hash == '#' || hash == '#/'){
                // already home: crossroads ignores a repeated identical request
                // unless its state is reset first
                crossroads.resetState();
                crossroads.parse('/');
            }else{
                window.location.hash = '#/';
            }
        });
    }
}
